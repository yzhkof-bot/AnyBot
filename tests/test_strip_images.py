"""测试图片裁剪逻辑 _strip_old_images"""
import sys
sys.path.insert(0, '.')

from server.agent.anthropic_adapter import AnthropicComputerUseAdapter
from server.agent.openai_adapter import OpenAICompatAdapter

# === Anthropic 适配器测试 ===
print('=== Anthropic 适配器 _strip_old_images 测试 ===')
messages = [
    {'role': 'user', 'content': [
        {'type': 'text', 'text': '打开Safari'},
        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': 'IMG1_DATA'}},
    ]},
    {'role': 'assistant', 'content': [{'type': 'text', 'text': '好的'}]},
    {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'abc', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'data': 'IMG2_DATA'}},
            {'type': 'text', 'text': 'ok'},
        ]},
    ]},
    {'role': 'assistant', 'content': [{'type': 'text', 'text': '继续'}]},
    {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'def', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'data': 'IMG3_DATA'}},
            {'type': 'text', 'text': 'done'},
        ]},
    ]},
]

result = AnthropicComputerUseAdapter._strip_old_images(messages)

# 验证第1张图被替换
assert result[0]['content'][1] == {'type': 'text', 'text': '[截图已省略]'}, 'IMG1 应被替换'
# 验证第2张图被替换
assert result[2]['content'][0]['content'][0] == {'type': 'text', 'text': '[截图已省略]'}, 'IMG2 应被替换'
# 验证第3张图（最后一张）保留
assert result[4]['content'][0]['content'][0]['type'] == 'image', 'IMG3 应保留'
# 验证文字内容不受影响
assert result[0]['content'][0]['text'] == '打开Safari', '文字不应变'
assert result[2]['content'][0]['content'][1]['text'] == 'ok', 'tool_result 文字不应变'
# 验证原始 messages 未被修改
assert messages[0]['content'][1]['type'] == 'image', '原始数据不应被修改'
print('✅ Anthropic: 图片裁剪正确，只保留最后一张，文字全部保留')

# === OpenAI 适配器测试 ===
print('\n=== OpenAI 适配器 _strip_old_images 测试 ===')
messages_oai = [
    {'role': 'system', 'content': '你是 AI'},
    {'role': 'user', 'content': [
        {'type': 'text', 'text': '打开Safari'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,IMG1'}},
    ]},
    {'role': 'assistant', 'content': '好的'},
    {'role': 'user', 'content': [
        {'type': 'text', 'text': '操作完成截图：'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,IMG2'}},
    ]},
    {'role': 'user', 'content': [
        {'type': 'text', 'text': '最新截图：'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,IMG3'}},
    ]},
]

result_oai = OpenAICompatAdapter._strip_old_images(messages_oai)
assert result_oai[1]['content'][1] == {'type': 'text', 'text': '[截图已省略]'}, 'IMG1 应被替换'
assert result_oai[3]['content'][1] == {'type': 'text', 'text': '[截图已省略]'}, 'IMG2 应被替换'
assert result_oai[4]['content'][1]['type'] == 'image_url', 'IMG3 应保留'
assert result_oai[1]['content'][0]['text'] == '打开Safari', '文字不应变'
assert messages_oai[1]['content'][1]['type'] == 'image_url', '原始数据不应被修改'
print('✅ OpenAI: 图片裁剪正确，只保留最后一张，文字全部保留')

# === 边界情况 ===
print('\n=== 边界情况测试 ===')
msgs_no_img = [{'role': 'user', 'content': 'hello'}]
assert AnthropicComputerUseAdapter._strip_old_images(msgs_no_img) is msgs_no_img
print('✅ 0 张图片 → 原样返回')

msgs_one_img = [{'role': 'user', 'content': [
    {'type': 'image', 'source': {'data': 'X'}}
]}]
assert AnthropicComputerUseAdapter._strip_old_images(msgs_one_img) is msgs_one_img
print('✅ 1 张图片 → 原样返回')

msgs_no_img_oai = [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}]
assert OpenAICompatAdapter._strip_old_images(msgs_no_img_oai) is msgs_no_img_oai
print('✅ OpenAI 0 张图片 → 原样返回')

print('\n🎉 所有测试通过！')
