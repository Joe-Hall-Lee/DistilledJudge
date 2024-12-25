import json
import re

def remove_templates(text):
    """移除模板标记，例如<|...|>"""
    return re.sub(r'<\|.*?\|>', '', text).strip()

def process_file(input_filename, output_filename):
    output_data = []
    toggle = True  # 初始化切换标志

    with open(input_filename, 'r', encoding='utf-8') as infile:
        for line_number, line in enumerate(infile, 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                print(f"行 {line_number} 无效 JSON，已跳过: {line}")
                continue

            # 提取 instruction
            prompt = data.get('prompt', '')
            instruction_match = re.search(
                r'<\|start_header_id\|>user<\|end_header_id\|>\n\n(.*?)<\|eot_id\|>',
                prompt,
                re.DOTALL
            )
            if instruction_match:
                instruction = remove_templates(instruction_match.group(1))
            else:
                instruction = ''

            # 处理 chosen 和 rejected
            chosen_text = remove_templates(data.get('chosen', ''))
            rejected_text = remove_templates(data.get('rejected', ''))

            # 根据 results 字段确定是否需要交换 chosen 和 rejected
            if data.get('results', 1) == 0:
                chosen_text, rejected_text = rejected_text, chosen_text

            # 根据 toggle 决定输出标签和文本内容的对应关系
            if toggle:
                output_label = "Output (a)"
                a_text = chosen_text
                b_text = rejected_text
            else:
                output_label = "Output (b)"
                a_text = rejected_text
                b_text = chosen_text

            # 设置 output 字段
            output = output_label

            # 创建新的数据条目
            output_entry = {
                "instruction": f"""Select the Output (a) or Output (b) that is better for the given instruction. The two outputs are generated by two different AI chatbots respectively.

Here are some rules of the evaluation:
(1) You should prioritize evaluating whether the output honestly/precisely/closely executes the instruction, then consider its helpfulness, accuracy, level of detail, harmlessness, etc.
(2) Outputs should NOT contain more/less than what the instruction asks for, as such outputs do NOT precisely execute the instruction.
(3) You should avoid any potential bias and your judgment should be as objective as possible. For example, the order in which the outputs were presented should NOT affect your judgment, as Output (a) and Output (b) are **equally likely** to be the better.

Do NOT provide any explanation for your choice.
Do NOT say both / neither are good.
You should answer using ONLY "Output (a)" or "Output (b)". Do NOT output any other words.

# Instruction:
{instruction}

# Output (a):
{a_text}

# Output (b):
{b_text}

# Which is better, Output (a) or Output (b)? Your response should be either "Output (a)" or "Output (b)":""",
                "input": "",

                "system": "You are a helpful assistant in evaluating the quality of the outputs for a given instruction. Your goal is to select the best output for the given instruction.",
                "output": output
            }
            output_data.append(output_entry)

            # 切换 toggle 标志，以便下一个条目时交换
            toggle = not toggle

    # 将结果写入输出文件
    with open(output_filename, 'w', encoding='utf-8') as outfile:
        for entry in output_data:
            json_line = json.dumps(entry, ensure_ascii=False)
            outfile.write(json_line + '\n')

if __name__ == '__main__':
    # 输入和输出文件名
    input_file = '../result/arena/output/Llama-3.2-3B-Instruct_test_len1024_fulltrain_1e-05_dataarena_dpo.json_outputs.jsonl'
    output_file = '../data/arena_et_with_rm.json'
    process_file(input_file, output_file)