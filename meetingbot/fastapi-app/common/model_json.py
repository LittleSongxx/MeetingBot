import json

from langchain_core.output_parsers import JsonOutputParser


class SchemaJsonParser(JsonOutputParser):
    """按给定的 JSON Schema 约束模型输出，并把模型返回的文本解析成 JSON。

    parse() 沿用 JsonOutputParser 的实现：能剥掉 ```json 代码块围栏，也能忽略 JSON 后面多出来的文字。
    get_format_instructions() 换成中文说明，本项目发给模型的提示词都是中文。
    """

    # 各个服务里定义的 SCHEMA 常量，只在拼格式说明时使用
    schema_dict: dict = {}

    def get_format_instructions(self) -> str:
        return (
            "\n\n输出要求：只返回一个 JSON 对象，不要使用 Markdown 代码块，不要输出任何解释文字。"
            "JSON 必须符合下面的结构定义：\n" + json.dumps(self.schema_dict, ensure_ascii=False)
        )


def build_schema_instruction(schema: dict) -> str:
    """把 JSON Schema 拼成一段附加提示，告诉模型必须输出哪些字段。

    结构定义只写在各个服务的 SCHEMA 常量里，管理员在页面上改 Prompt 模板时
    不会影响这段结构约束。
    """
    return SchemaJsonParser(schema_dict=schema).get_format_instructions()


def parse_model_json(text: str) -> dict:
    """把模型返回的文本解析成字典，空文本、非 JSON 文本、JSON 数组都会抛出异常。"""
    result = SchemaJsonParser().parse(text or "")
    if not isinstance(result, dict):
        raise RuntimeError("模型返回的不是JSON对象")
    return result
