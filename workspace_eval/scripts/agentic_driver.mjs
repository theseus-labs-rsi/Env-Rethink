#!/usr/bin/env node
/**
 * agentic_driver.mjs — 极简 Claude Agent SDK driver for noise-id agentic rollout.
 *
 *   node agentic_driver.mjs <cfg.json> -o <report.json>
 *
 * cfg.tasks[0]: {id, prompt, cwd, timeout(s), maxTurns, provider, model,
 *                customProvider:{baseUrl, apiKey, modelName}}
 *
 * 用 SDK query() 直驱 Claude Code CLI；把流式消息归一成与 agent.json.executionTrace
 * 同形的 events（text / tool），并给出最终 assistant 文本与 usage。不写 HOME/CLAUDE_CONFIG_DIR，
 * 避免无关环境差异。CLI 本体的 env（ANTHROPIC_* 等）来自 customProvider + process.env。
 *
 * 状态语义：完成（含正常结束 / 手动工具完成）→ passed；超时 → timeout；
 * 抛错 / SDK result 报告 error 或 maxTurns → error。maxTurns 用尽必须上报 error，
 * 不能当作 passed（否则下游会把截断的判定当有效结果）。
 */
import fs from 'fs';
import path from 'path';
import { query } from '../../evaluation/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs';

const SDK_CLI = path.resolve(import.meta.dirname, '..', '..',
  'evaluation/node_modules/@anthropic-ai/claude-agent-sdk/cli.js');

function nowIso() { return new Date().toISOString(); }

function readCfg(p) { return JSON.parse(fs.readFileSync(p, 'utf8')); }

async function runOne(task) {
  const startedAt = new Date();
  const events = [];
  const callIndex = {};            // callID -> tool event
  const texts = [];
  let usage = null, errorMessage = null, status = 'passed';
  let isErrorResult = false, resultError = null;

  const env = { ...process.env };
  if (task.customProvider) {
    env.ANTHROPIC_AUTH_TOKEN = task.customProvider.apiKey;
    env.ANTHROPIC_API_KEY = task.customProvider.apiKey;
    // Claude Code 会自己拼 /v1/messages，base 不能带 /v1
    let base = String(task.customProvider.baseUrl || '').trim().replace(/\/+$/, '');
    if (base.endsWith('/v1')) base = base.slice(0, -3).replace(/\/+$/, '');
    env.ANTHROPIC_BASE_URL = base;
    env.ANTHROPIC_MODEL = task.customProvider.modelName;
  }
  if (task.env && typeof task.env === 'object') Object.assign(env, task.env);
  // Gemini-3.7 这类 thinking 模型在工具环下必须带 thinking/effort，否则网关 500；
  // 由 cfg.model 触发（与 runner 的 --model 一致）。
  const modelLower = String(task.model || (task.customProvider && task.customProvider.modelName) || '').toLowerCase();
  if (modelLower.includes('gemini') || modelLower.includes('glm') || modelLower.includes('kimi')) {
    if (!env.CLAUDE_CODE_EFFORT_LEVEL) env.CLAUDE_CODE_EFFORT_LEVEL = 'max';
  }
  const timeoutSec = task.timeout ?? 900;
  const maxTurns = Number.isInteger(task.maxTurns) && task.maxTurns > 0 ? task.maxTurns : undefined;
  const ac = new AbortController();
  const timer = setTimeout(() => { status = 'timeout'; ac.abort(); }, timeoutSec * 1000);

  try {
    const opts = {
      prompt: task.prompt,
      options: {
        cwd: task.cwd ?? process.cwd(),
        abortController: ac,
        env,
        pathToClaudeCodeExecutable: SDK_CLI,
        permissionMode: 'default',
        maxTurns,
        includePartialMessages: false,
        // DeepSeek 网关不接受 Read 二进制/PDF 返回的图片内容块 → 拒绝此类 Read，
        // 强制走 pdftotext / soffice / tesseract 等 Bash 读（镜像已装）。
        settings: { permissions: { ask: ['Read'] } },
        canUseTool: async (toolName, input) => {
          if (toolName === 'Read') {
            const p = String((input && (input.file_path || input.path)) || '').toLowerCase();
            if (/\.(pdf|png|jpe?g|gif|bmp|webp|heic)$/.test(p)) return false;
          }
          return true;
        },
      },
    };
    for await (const msg of query(opts)) {
      if (msg.type === 'assistant') {
        for (const b of msg.message?.content ?? []) {
          if (b.type === 'text') {
            const ev = { type: 'text', role: 'assistant', content: b.text, timestamp: nowIso() };
            events.push(ev); texts.push(b.text);
          } else if (b.type === 'tool_use') {
            const ev = { type: 'tool', role: 'tool', tool: b.name, name: b.name, callID: b.id,
                         status: 'running', input: b.input, output: null, timestamp: nowIso() };
            events.push(ev); callIndex[b.id] = ev;
          }
        }
      } else if (msg.type === 'user') {
        for (const b of msg.message?.content ?? []) {
          if (b.type === 'tool_result' && b.tool_use_id && callIndex[b.tool_use_id]) {
            const ev = callIndex[b.tool_use_id];
            const c = b.content;
            const text = typeof c === 'string' ? c
              : Array.isArray(c) ? c.map(x => (x && x.type === 'text') ? x.text : '').join('\n') : '';
            ev.status = b.is_error ? 'failed' : 'completed';
            ev.output = text;
            ev.is_error = Boolean(b.is_error);
          } else if (b.type === 'text') {
            events.push({ type: 'text', role: 'user', content: b.text, timestamp: nowIso() });
          }
        }
      } else if (msg.type === 'result') {
        usage = msg.usage ?? null;
        const r = msg.result ?? null;
        // 防御性兼容不同 SDK 版本的终止原因字段；命中任一即视为失败终止。
        const bad = Boolean(
          (r && (r.isError || r.isTurnLimitExceeded || r.type === 'error'
                 || r.type === 'error_max_turns'))
          || msg.isError
          || msg.subtype === 'error_max_turns'
          || msg.subtype === 'error');
        if (bad) {
          isErrorResult = true;
          resultError = (r && r.error) ?? msg.error ?? null;
        }
      }
    }
  } catch (e) {
    if (status !== 'timeout') status = 'error';
    errorMessage = (e && e.message) ? String(e.message) : String(e);
  } finally {
    clearTimeout(timer);
  }

  // 正常流结束后才报出的 error / maxTurns：覆写 passed
  if (status === 'passed' && isErrorResult) {
    status = 'error';
    errorMessage = errorMessage ?? (resultError ? String(resultError)
      : 'SDK result 报告 error / 达到 maxTurns，判定截断');
  }

  const finishedAt = new Date();
  const toolCalls = events.filter(e => e.type === 'tool');
  return {
    id: task.id, name: task.name || task.id, status,
    provider: task.provider ?? null, model: task.model ?? null,
    cwd: task.cwd ?? process.cwd(), timeout: timeoutSec,
    startedAt: startedAt.toISOString(), finishedAt: finishedAt.toISOString(),
    durationMs: finishedAt.getTime() - startedAt.getTime(),
    eventCount: events.length, errorMessage,
    trajectory: events, toolCalls, textOutputs: texts,
    finalText: texts.length ? texts[texts.length - 1] : '',
    usage,
  };
}

async function main() {
  const args = process.argv.slice(2);
  const cfgPath = args[0];
  let out = null;
  const oi = args.indexOf('-o');
  if (oi >= 0 && args[oi + 1]) out = args[oi + 1];
  if (!cfgPath || !fs.existsSync(cfgPath)) {
    console.error('usage: agentic_driver.mjs <cfg.json> [-o report.json]');
    process.exit(2);
  }
  const cfg = readCfg(cfgPath);
  if (!Array.isArray(cfg.tasks) || cfg.tasks.length === 0) {
    console.error(`cfg.tasks 为空: ${cfgPath}`);
    process.exit(2);
  }
  if (cfg.tasks.length > 1) {
    console.warn(`cfg.tasks 含 ${cfg.tasks.length} 项，driver 仅执行首个`);
  }
  const task = cfg.tasks[0];
  const rep = await runOne(task);
  rep.description = cfg.description ?? null;
  if (out) fs.writeFileSync(out, JSON.stringify(rep, null, 2));
  else process.stdout.write(JSON.stringify(rep, null, 2));
}

main().catch(e => { console.error(e); process.exit(1); });
