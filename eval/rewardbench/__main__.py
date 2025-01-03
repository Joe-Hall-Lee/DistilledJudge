# Copyright 2023 AllenAI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Run RewardBench (evaluate any reward model on any dataet)
import argparse
import json
import logging
import os
import sys

import torch
import transformers
from accelerate import Accelerator
from accelerate.logging import get_logger
from tqdm import tqdm
from transformers import AutoTokenizer

from rewardbench import (
    REWARD_MODEL_CONFIG,
    check_tokenizer_chat_template,
    load_preference_dataset,
)


def main():
    parser = argparse.ArgumentParser(description="Evaluate a reward model.")

    # core args
    parser.add_argument("--dataset", type=str, required=True, help="The dataset to evaluate on.")
    parser.add_argument("--split", type=str, default=None, help="The split to evaluate on.")
    parser.add_argument("--model", type=str, required=True, help="The model to evaluate.")
    parser.add_argument("--tokenizer", type=str, default=None, help="The tokenizer to use (defaults to model).")
    parser.add_argument(
        "--chat_template",
        type=str,
        default=None,
        help="The chat template to use (defaults to from tokenizer, from chattemplate).",
    )

    # inference args
    parser.add_argument("--batch_size", type=int, default=8, help="The batch size to use.")
    parser.add_argument("--max_length", type=int, default=512, help="The max length to use.")

    # system args
    parser.add_argument("--load_json", action="store_true", default=False, help="Load dataset as json.")
    parser.add_argument("--trust_remote_code", action="store_true", default=False, help="Trust remote code.")
    parser.add_argument("--debug", action="store_true", default=False, help="Debug mode.")
    parser.add_argument("--output_dir", type=str, default="results/", help="The output directory to save results.")
    parser.add_argument("--save_all", action="store_true", default=False, help="Save all results.")
    args = parser.parse_args()

    ###############
    # Setup logging
    ###############
    accelerator = Accelerator()
    current_device = accelerator.process_index

    logger = get_logger(__name__)
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    log_level = logging.INFO
    logger.setLevel(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    logger.info(f"Running reward model on {args.model} with chat template {args.chat_template}")
    if args.trust_remote_code:
        logger.info("Loading model with Trust Remote Code")

    # basic checks from config
    MODEL_CONFIGS = REWARD_MODEL_CONFIG

    if args.chat_template:
        from fastchat.conversation import get_conv_template

        conv = get_conv_template(args.chat_template)
    else:
        conv = None

    if args.model in MODEL_CONFIGS:
        config = MODEL_CONFIGS[args.model]
    else:
        config = MODEL_CONFIGS["default"]
    logger.info(f"Using reward model config: {config}")

    # Default entries
    # "model_builder": AutoModelForSequenceClassification.from_pretrained,
    # "pipeline_builder": pipeline,
    # "quantized": True,
    # "custom_dialogue": False,
    # "model_type": "Seq. Classifier"

    quantized = config["quantized"]
    custom_dialogue = config["custom_dialogue"]
    pipeline_builder = config["pipeline_builder"]
    _ = config["model_type"]
    if custom_dialogue:
        raise NotImplementedError("Custom dialogue not implemented yet for simpler data formatting.")

    model_builder = config["model_builder"]

    #########################
    # load dataset
    #########################
    logger.info("*** Load dataset ***")
    tokenizer_path = args.tokenizer if args.tokenizer else args.model
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, trust_remote_code=args.trust_remote_code)
    dataset = load_preference_dataset(
        args.dataset, split=args.split, json=args.load_json, tokenizer=tokenizer, conv=conv
    )

    if args.debug:
        dataset = dataset.select(range(10))

    logger.info("*** Load reward model ***")

    ############################
    # Load classifier model pipeline
    ############################
    reward_pipeline_kwargs = {
        "batch_size": args.batch_size,  # eval_args.inference_batch_size,
        "truncation": True,
        "padding": True,
        "max_length": args.max_length,
        "function_to_apply": "none",  # Compute raw logits
        "return_token_type_ids": False,
    }
    if quantized:
        model_kwargs = {
            "load_in_8bit": True,
            "device_map": {"": current_device},
            "torch_dtype": torch.float16 if torch.cuda.is_available() else None,
        }
    else:
        model_kwargs = {"device_map": {"": current_device}}

    model = model_builder(args.model, **model_kwargs, trust_remote_code=args.trust_remote_code)
    reward_pipe = pipeline_builder(
        "text-classification",  # often not used
        model=model,
        tokenizer=tokenizer,
    )

    # set pad token to eos token if not set
    if reward_pipe.tokenizer.pad_token_id is None:
        reward_pipe.model.config.pad_token_id = reward_pipe.tokenizer.eos_token_id
        reward_pipe.tokenizer.pad_token_id = reward_pipe.tokenizer.eos_token_id
    # For models whose config did not contains `pad_token_id`
    if reward_pipe.model.config.pad_token_id is None:
        reward_pipe.model.config.pad_token_id = reward_pipe.tokenizer.pad_token_id

    # if using fastchat template (no template in tokenizer), make the RM tokenizer output an EOS token
    if not check_tokenizer_chat_template(tokenizer):
        reward_pipe.tokenizer.add_eos_token = True

    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=False,
        drop_last=False,
    )

    dataloader, model = accelerator.prepare(dataloader, reward_pipe.model)
    reward_pipe.model = model

    ############################
    # Run inference
    ############################

    results = []
    scores_chosen = []
    scores_rejected = []
    for step, batch in enumerate(tqdm(dataloader, desc="RM batch steps")):
        logger.info(f"RM inference step {step}/{len(dataloader)}")

        rewards_chosen = reward_pipe(batch["text_chosen"], **reward_pipeline_kwargs)
        rewards_rejected = reward_pipe(batch["text_rejected"], **reward_pipeline_kwargs)

        # for each item in batch, record 1 if chosen > rejected
        # extra score from dict within batched results (e.g. logits)
        # [{'label': 'LABEL_1', 'score': 0.6826171875},... ]
        if isinstance(rewards_chosen[0], dict):
            score_chosen_batch = [result["score"] for result in rewards_chosen]
            score_rejected_batch = [result["score"] for result in rewards_rejected]
        # for classes that directly output scores (custom code)
        else:
            score_chosen_batch = rewards_chosen.cpu().numpy().tolist()
            score_rejected_batch = rewards_rejected.cpu().numpy().tolist()

        # log results
        [
            results.append(1) if chosen > rejected else results.append(0)
            for chosen, rejected in zip(score_chosen_batch, score_rejected_batch)
        ]
        scores_chosen.extend(score_chosen_batch)
        scores_rejected.extend(score_rejected_batch)

    ############################
    # compile scores
    ############################
    # calculate accuracy
    accuracy = sum(results) / len(results)
    logger.info(f"Results: {accuracy}, on {len(results)} prompts")

    ############################
    # compile scores
    ############################
    # save score in json to args.output_dir + args.model + ".json"
    output_path = args.output_dir + args.model + ".json"
    dirname = os.path.dirname(output_path)
    os.makedirs(dirname, exist_ok=True)

    # remove old data
    if os.path.exists(output_path):
        os.remove(output_path)

    with open(output_path, "w") as f:
        json.dump(
            {
                "accuracy": accuracy,
                "num_prompts": len(results),
                "model": args.model,
                "tokenizer": tokenizer_path,
                "chat_template": args.chat_template,
            },
            f,
        )

    # if save_all is passed, save a large jsonl with all scores_chosen, scores_rejected
    if args.save_all:
        output_path = args.output_dir + args.model + "_all.jsonl"
        dirname = os.path.dirname(output_path)
        os.makedirs(dirname, exist_ok=True)

        # remove old data
        if os.path.exists(output_path):
            os.remove(output_path)

        with open(output_path, "w") as f:
            for chosen, rejected in zip(scores_chosen, scores_rejected):
                f.write(json.dumps({"chosen": scores_chosen, "rejected": scores_rejected}) + "\n")


if __name__ == "__main__":
    main()
