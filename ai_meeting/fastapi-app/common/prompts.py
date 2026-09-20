"""提示词模板的变量渲染。

唯一的实现：纪要生成、说话人匹配、纪要自检三处共用，此前在
minutes_service 与 speaker_service 里各写了一份逐字相同的代码。
"""


def render_prompt(template: str, **variables) -> str:
    result = template
    # 模板里出现的 {key} 换成传入的字符串，没有传值的变量原样保留
    for key, value in variables.items():
        result = result.replace("{" + key + "}", value)
    return result
