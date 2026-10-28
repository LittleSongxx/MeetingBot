import json, urllib.request, urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
ENV=ROOT/"meetingbot"/"fastapi-app"/".env"
env={}
for line in ENV.read_text(encoding="utf-8").splitlines():
    line=line.strip()
    if not line or line.startswith("#") or "=" not in line: continue
    k,v=line.split("=",1); env[k.strip()]=v.strip().strip('"').strip("'")
BASE="http://127.0.0.1:19091"
def req(path, method="GET", payload=None, token=""):
    data=json.dumps(payload).encode() if payload is not None else None
    r=urllib.request.Request(BASE+path,data=data,method=method)
    r.add_header("Content-Type","application/json;charset=utf-8")
    if token: r.add_header("token",token)
    with urllib.request.urlopen(r,timeout=120) as resp: return resp.status,resp.read().decode()
_,body=req("/login","POST",{"username":env["APP_ADMIN_USERNAME"],"password":env["APP_ADMIN_PASSWORD"]})
token=json.loads(body)["data"]["token"]
_,body=req("/promptTemplate/selectPage?pageNum=1&pageSize=20",token=token)
row=next(x for x in json.loads(body)["data"]["list"] if x["code"]=="CHAT_ANSWER")
Path(ROOT/"evaluation"/"results"/"prompt_CHAT_ANSWER_p1c_20261028.json").write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding="utf-8")
new_system=("你是企业会议知识助手。只使用工具返回的会议资料回答；第一句必须完整直接回答用户问题（含问题中的对象/时间/条件；否定/未确认也要先回应）。"
 "随后最多 1–2 句必要依据，且必须有 [n] 引用直接支持；没有引用支持的背景、评价、过程、推断性描述一律不写；宁短勿长。"
 "每条事实性结论必须紧贴引用编号 [n]；推断、建议、常识必须显式标为【推断】，不得写成会议事实；"
 "数字、日期、人名、状态词必须与引用内容一致，不得改写或推断；不要输出 meeting_id/source_id 等内部标识，不要单列“依据/来源”段落；"
 "不要写“如需进一步检索/建议补充查询”这类套话；工具结果已足够时直接作答；资料不足时只说明缺失项，不追加检索。")
new_user=("回答要求：第一句完整直接回答（含问题对象/时间/条件）；随后最多 1–2 句必要依据，每句必须有 [n] 支持；没有引用支持的句子不要写；"
 "单事实题不超过 3 句；清单/多事实问题逐条列出；不重复抄写引用原文；不写“以上信息来自…/依据：/来源：”这类元陈述；"
 "不写建议式套话；资料不足时用一句话说明缺什么并直接说明当前无法确认。")
payload={"id":row["id"],"code":row["code"],"name":row["name"],"scene_type":row["scene_type"],
 "system_prompt":new_system,"user_prompt":new_user,"enabled":True}
code,body=req("/promptTemplate/update","PUT",payload,token)
print("update",code,body[:100])
