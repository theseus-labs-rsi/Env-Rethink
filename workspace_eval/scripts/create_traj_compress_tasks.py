#!/usr/bin/env python3
"""创建轨迹压缩的 远程伪任务。

对超长的 CA 轨迹,让 deepseek 智能压缩(合并重复读取、精简文件摘录、保留判定),
使总长度降到 ~40K tokens 以内。
"""
import json, glob, os, shutil
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
D1 = str(EVAL/'experiments/ca-teacher-rollout-full-ebd7fe2e-20260913T134239Z')
D2 = str(EVAL/'experiments/ca-teacher-rollout-r2-ebd7fe2e-20260913T170557Z')
OUT = EVAL/'.generated'/'traj_compress_tasks'
GEN = EVAL/'.generated'/'noise_id_subenvs'

PROMPT = """你是一个轨迹压缩器。你的任务是压缩一份「文件核验 agent 的执行轨迹」,使其总长度大幅缩短,同时保留核心学习信号。

## 输入
工作区中的 `trajectory.json` 是原始轨迹,格式为 messages 数组:
- user 消息(1 条):任务提示 → **保持原样,一个字不改**
- assistant 消息(N 条):中间推理 + 工具调用 → 可压缩
- tool 消息(N 条):工具返回(文件内容)→ 可大幅压缩

## 压缩规则(优先级从高到低)
1. **最终判定**(最后一条 assistant 消息,含 JSON)→ 完整保留
2. **工具调用参数**(command/file_path)→ 保留调用结构,参数保持原样(模型要学"怎么调工具")
3. **工具返回内容**(文件内容)→ 压缩到 ≤300 字符的关键摘录:
   - 保留:数值、日期、编号、名称、结论性语句
   - 删除:冗余正文、格式空白、重复内容
4. **中间推理文本**(assistant 的叙述)→ 压缩到 ≤100 字符
5. **合并同类**:如果 agent 多次读取同一文件或同族文件,可合并为一次调用(保留最完整的一次)

## 输出
把压缩后的完整 messages JSON 写到 `model_output/compressed.json`,格式与输入相同(每条 message 有 role 和 content 字段;工具调用的 content 包含原生格式 `<tool_call>` 文本)。

## 约束
- 输出的 messages 总字符数 ≤ 80000(约 40K tokens)
- 必须包含:1 条 user + ≥3 条 assistant(含最终判定) + 对应的 tool 消息
- 用 `wc -c model_output/compressed.json` 确认大小
"""

def build():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    made = 0
    for D in [D1, D2]:
        for mf in sorted(glob.glob(f'{D}/cases/task*-ca*/agent/output/noise_labels.json')):
            parts = mf.split('/')
            sid = parts[-4].replace('task','')
            base_sid = sid.split('-r')[0] if '-r' in sid else sid
            # 只处理合格的
            try:
                pred_raw = json.load(open(mf))
                pred = {f['path'].lstrip('/'): f for f in pred_raw.get('files',[]) if isinstance(f,dict) and f.get('path')}
            except: continue
            gt_path = f'{GEN}/{base_sid}/labels.json'
            if not os.path.isfile(gt_path): continue
            gt = {f['path'].lstrip('/'): f for f in json.load(open(gt_path))['files']}
            std_kill = 0
            for p,g in gt.items():
                if p not in pred: continue
                pp = pred[p].get('partition')
                if g['partition']=='standard' and pp!='standard': std_kill+=1
            if std_kill>1: continue

            # 只处理超长的(build_dataset 在 44K 会拒绝的那些)
            aj = os.path.join(os.path.dirname(os.path.dirname(mf)), 'agent.json')
            a = json.load(open(aj))
            tr = a.get('trace',{}).get('executionTrace') or []
            # 粗估 token(CJK 感知:字符/2)
            chars = sum(len(c) if isinstance(c,str) else len(str(c)) for m in tr for c in ([m.get('content')] if isinstance(m.get('content'),str) else (m.get('content') or []) if isinstance(m.get('content'),list) else ['']))
            est = int(chars/2)
            if est < 44000: continue

            # 提取 messages(简化版:直接用 trace 的 role/content)
            msgs = []
            for m in tr:
                role = m.get('role') or m.get('type')
                c = m.get('content')
                if isinstance(c, list):
                    txt = ''
                    for b in c:
                        if isinstance(b,dict) and b.get('type')=='text': txt += b.get('text','')
                        elif isinstance(b,dict) and b.get('type')=='tool_result':
                            rc = b.get('content')
                            if isinstance(rc,str): txt += rc
                            elif isinstance(rc,list):
                                for rb in rc:
                                    if isinstance(rb,dict) and rb.get('type')=='text': txt += rb.get('text','')
                    c = txt
                if isinstance(c,str) and c.strip():
                    msgs.append({'role': role, 'content': c})

            # 写入伪任务
            out_dir = OUT / f'{sid}-compress'
            data = out_dir / 'data'
            data.mkdir(parents=True)
            with open(data/'trajectory.json','w') as f:
                json.dump(msgs, f, ensure_ascii=False)
            meta = {
                'id': f'{sid}-compress',
                'language': 'cn',
                'file_system': 'dataseed',
                'task': PROMPT,
                'output_files': ['compressed.json'],
                'rubrics': ['占位'],
                'rubric_types': ['占位'],
                'data_manifest': [{'filename':'trajectory.json','stored_relpath':'data/trajectory.json','target_path':'trajectory.json','input_role':'noise'}],
            }
            (out_dir/'metadata.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1))
            made += 1
    print(f'{made} 个压缩任务 -> {OUT}')

if __name__ == '__main__':
    build()
