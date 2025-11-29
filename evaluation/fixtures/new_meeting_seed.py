import json
import os

import pymysql

SEG_A = [
 ("李娜","各位上午好，今天评审客户支持知识库和智能客服灰度的准备情况，先对齐目标和风险。",0,8000),
 ("王磊","当前知识库覆盖率为82%，我们的目标是把覆盖率提升到90%，重点补齐两个未覆盖主题：账单纠纷和权限申请。",8000,18000),
 ("王磊","我计划新增120条FAQ，负责人是我，截止10月25日；每条FAQ都需要来源和审核人。",18000,28000),
 ("陈雪","智能客服灰度定在11月1日，先放量10%，回滚预案要求15分钟内切回旧版。",28000,38000),
 ("陈雪","灰度依赖账单API，账单API的稳定性验证要在10月30日前完成，否则灰度顺延。",38000,47000),
 ("张敏","客户满意度CSAT目前4.3，目标提升到4.5；这个季度支持预算8万元。",47000,56000),
 ("张敏","主要风险是退款政策已过期，知识库如果继续引用旧政策会误导客户。",56000,65000),
 ("李娜","今天先确认知识库补齐和灰度前检查两项决策，具体行动项下午继续对齐。",65000,72000),
]
SEG_B = [
 ("李娜","下午继续，先复盘上午确定的知识库和灰度目标。",0,8000),
 ("王磊","FAQ已经拆分到四个主题，账单纠纷和权限申请是优先级最高的两个；10月25日交付。",8000,18000),
 ("陈雪","11月1日灰度10%，如果账单API在10月30日前没有通过稳定性验证，就推迟灰度。",18000,30000),
 ("陈雪","全量上线暂定11月15日，前提是灰度期没有P1问题；回滚15分钟。",30000,40000),
 ("张敏","退款政策文档10月28日前由我更新，客服话术也同步调整。",40000,48000),
 ("李娜","确认行动项：王磊FAQ 10月25日、陈雪API检查10月28日、张敏退款政策10月28日；10月22日再开一次检查会。",48000,58000),
 ("王磊","知识库每条FAQ要标来源和审核人，避免旧政策再次进入检索。",58000,66000),
 ("李娜","今天的决定是：知识库补齐后再评估灰度；灰度前必须完成API验证；全量上线不早于11月15日。",66000,74000),
]

conn=pymysql.connect(host=os.environ["MYSQL_HOST"],user=os.environ["MYSQL_USER"],
                     password=os.environ["MYSQL_PASSWORD"],database=os.environ["MYSQL_DATABASE"],
                     charset="utf8mb4",cursorclass=pymysql.cursors.DictCursor,autocommit=True)
cur=conn.cursor()
cur.execute("DELETE FROM transcript_segment WHERE meeting_id=3")
cur.execute("DELETE FROM transcription_task WHERE meeting_id=3")
cur.execute("DELETE FROM meeting_minutes WHERE meeting_id=3")
cur.execute("DELETE FROM meeting WHERE id=3")
cur.execute("INSERT INTO meeting (id,meeting_no,title,start_time,end_time,location,agenda,creator_id,host_id,status,series_id,create_time,update_time) VALUES (3,%s,%s,%s,%s,%s,%s,1,1,%s,NULL,NOW(),NOW())",
            ("MTG20261015090000000001","客户支持知识库与智能客服灰度评审会","2026-10-15 09:00:00","2026-10-15 16:00:00","线上会议","知识库覆盖与智能客服灰度", "IN_PROGRESS"))
def insert_task(task_id, recorded_at, segs):
    full="\n".join(f"[{start//1000}s][{speaker}] {text}" for speaker,text,start,end in segs)
    cur.execute("INSERT INTO transcription_task (id,meeting_id,material_id,model_config_id,initiator_id,language,status,stage,progress,retry_count,max_retry,total_duration_ms,full_text,started_at,finished_at,create_time,update_time,recorded_at) VALUES (%s,3,%s,1,1,%s,%s,%s,100,0,3,%s,%s,NOW(),NOW(),NOW(),NOW(),%s)",
                (task_id,100+task_id,"zh","SUCCEEDED","转写完成",segs[-1][3],full,recorded_at))
    for no,(speaker,text,start,end) in enumerate(segs,1):
        cur.execute("INSERT INTO transcript_segment (task_id,meeting_id,segment_no,start_ms,end_ms,speaker_label,original_text,content,create_time,update_time) VALUES (%s,3,%s,%s,%s,%s,%s,%s,NOW(),NOW())",
                    (task_id,no,start,end,speaker,text,text))
insert_task(8,"2026-10-15 09:00:00",SEG_A)
insert_task(9,"2026-10-15 14:00:00",SEG_B)
summary="会议确认客户支持知识库当前覆盖率82%、目标90%，未覆盖主题为账单纠纷和权限申请；计划新增120条FAQ（王磊，10月25日）。智能客服灰度11月1日10%，回滚15分钟，依赖账单API，API稳定性验证10月30日前完成；全量上线不早于11月15日。CSAT目标4.5，季度预算8万元，退款政策10月28日前更新。"
topics=[{"title":"知识库覆盖率与补齐计划","summary":"覆盖率82%→90%，补齐账单纠纷和权限申请，新增120条FAQ。"},
        {"title":"智能客服灰度方案","summary":"11月1日灰度10%，回滚15分钟；依赖账单API，10月30日前验证；全量不早于11月15日。"},
        {"title":"客户满意度和风险","summary":"CSAT目标4.5，预算8万；退款政策过期是主要风险，10月28日前更新。"}]
decisions=[{"basis":"知识库覆盖不足且退款政策存在过期风险","content":"知识库补齐后再评估智能客服灰度","owner_suggestion":"王磊","deadline_suggestion":"2026-10-25T18:00:00"},
           {"basis":"灰度依赖账单API稳定性","content":"灰度前必须完成账单API稳定性验证；11月1日先放量10%，回滚15分钟","owner_suggestion":"陈雪","deadline_suggestion":"2026-10-30T18:00:00"},
           {"basis":"灰度期稳定性与P1问题要求","content":"全量上线不早于11月15日","owner_suggestion":"陈雪","deadline_suggestion":"2026-11-15T18:00:00"}]
pending=[{"content":"新增120条FAQ，覆盖账单纠纷和权限申请","owner_suggestion":"王磊","deadline_suggestion":"2026-10-25T18:00:00"},
         {"content":"完成账单API稳定性验证","owner_suggestion":"陈雪","deadline_suggestion":"2026-10-30T18:00:00"},
         {"content":"更新退款政策文档和客服话术","owner_suggestion":"张敏","deadline_suggestion":"2026-10-28T18:00:00"}]
risks=[{"level":"HIGH","content":"退款政策已过期，知识库继续引用旧政策会误导客户","suggestion":"10月28日前更新退款政策并标注审核人"},
       {"level":"MEDIUM","content":"账单API未在10月30日前通过稳定性验证会导致灰度顺延","suggestion":"每日检查API稳定性，未通过则推迟灰度"}]
viewpoints=[{"speaker":"王磊","viewpoint":"知识库覆盖率82%，目标90%；新增120条FAQ，覆盖账单纠纷和权限申请，10月25日完成。"},
            {"speaker":"陈雪","viewpoint":"智能客服11月1日灰度10%，回滚15分钟；依赖账单API，10月30日前完成验证；全量上线不早于11月15日。"},
            {"speaker":"张敏","viewpoint":"CSAT目标4.5，预算8万元；退款政策已过期，10月28日前更新。"}]
cur.execute("INSERT INTO meeting_minutes (id,meeting_id,source_task_id,model_config_id,status,stage,progress,summary,topics_json,viewpoints_json,decisions_json,pending_items_json,risks_json,created_by,confirmed_by,confirmed_time,create_time,update_time) VALUES (5,3,8,2,%s,%s,100,%s,%s,%s,%s,%s,%s,1,1,NOW(),NOW(),NOW())",
            ("CONFIRMED","纪要已确认",summary,json.dumps(topics,ensure_ascii=False),json.dumps(viewpoints,ensure_ascii=False),json.dumps(decisions,ensure_ascii=False),json.dumps(pending,ensure_ascii=False),json.dumps(risks,ensure_ascii=False)))
print("new meeting inserted", cur.rowcount)
conn.close()
