# InsightMon 埋点接入文档

本文说明其他服务怎样把对话数据（trace、会话、版本、用户反馈）上报到 InsightMon AI Eval，供会话分析、Badcase、
回归验证使用。

- 平台地址（生产）：`https://insightmon-ai-eval-lulirrtvya-ew.a.run.app`（下文记作 `$INSIGHTMON_BASE_URL`）
- 可用性检查：`GET $INSIGHTMON_BASE_URL/api/v1/meta` 返回 200 即可用。**不要用 `/healthz`**（Cloud Run 会拦截以 `z`
  结尾的路径）
- Python SDK：[InsightMon/insightmon-eval-sdk](https://github.com/InsightMon/insightmon-eval-sdk)（完整参考见其 README、
  `docs/conventions.md`、`docs/contract.md`）

---

## 0. 结论：不是 LangGraph 的服务能不能采集？

**能。** 平台和 SDK 都不依赖 LangGraph 或任何 Agent 框架：

- 平台的接入口是标准 **OpenTelemetry（OTLP/HTTP）**，外加一个会话导入 REST 接口。谁发来的 span 都一样处理，只看
  span 上的属性（会话键、agent 版本等）。
- Python SDK 只依赖 OpenTelemetry 和 httpx，没有任何 LangGraph / LangChain 相关代码。你在自己的代码里用
  `insightmon.thread()`、`insightmon.turn()` 和普通的 OTel span 手动埋点即可。
- 非 Python 服务（Java、Go、Node.js 等）用各语言官方的 OpenTelemetry SDK，或者直接 HTTP POST JSON。

按你的情况选一条路径：

| 路径 | 适合 | 语言 | 实时性 | 见 |
|---|---|---|---|---|
| A. Python SDK | Python 服务，推荐 | Python ≥ 3.10 | 实时（每 5 秒批量导出） | [§2](#2-路径-apython-sdk推荐) |
| B. 标准 OpenTelemetry | 已有或愿意用 OTel 的非 Python 服务 | 任意 | 实时 | [§3](#3-路径-b任意语言的-opentelemetry-sdk) |
| C. 直接 HTTP 发 OTLP/JSON | 不想引入 OTel 依赖，自己拼 JSON | 任意（curl 即可） | 实时 | [§4](#4-路径-c直接-http-上报-otlpjson) |
| D. 会话导入 | 会话结束后整段补录、离线批量导入历史数据 | 任意 | 批量 | [§5](#5-路径-d整段会话导入) |

本文的每个示例（Python SDK、Node.js OpenTelemetry、curl 发 OTLP/JSON、会话导入、用户反馈）都已在当前 main 的
平台实例上实测通过：会话建成、轮次与工具 span 正确，agent 版本与组件版本、语言、用户引用、metadata、显式关闭
会话都按预期落库。

---

## 1. 准备工作

### 1.1 建 API Key

在平台上打开项目 → 侧栏「API Key」（需要项目管理员）→ 新建 Key，按用途勾选权限：

| 权限（界面显示） | scope | 用途 |
|---|---|---|
| 上报 Trace（OTLP） | `trace_ingest` | 路径 A / B / C（`/otlp/v1/traces`、`/otlp/v1/logs`） |
| 导入会话等数据 | `ingest` | 路径 D（`/api/v1/threads:import`） |
| 写入 outcome / end_user / import 来源的 Score | `score_write` | 上报用户反馈、业务结果（[§7](#7-上报用户反馈与业务结果可选)） |

密钥以 `imk_` 开头，**只显示一次**。放进服务的密钥管理（环境变量 / Secret Manager），不要写进代码或提交到仓库。
Key 只属于一个项目，请求里不需要再带项目 id。

### 1.2 环境变量

```bash
export INSIGHTMON_BASE_URL=https://insightmon-ai-eval-lulirrtvya-ew.a.run.app
export INSIGHTMON_API_KEY=imk_xxxxxxxx      # 占位符，换成你的 Key
```

自检：

```bash
curl -s "$INSIGHTMON_BASE_URL/api/v1/meta"                                   # 平台可用
curl -s -H "Authorization: Bearer $INSIGHTMON_API_KEY" \
     "$INSIGHTMON_BASE_URL/api/v1/api-key"                                   # Key 的项目与权限
```

### 1.3 数据模型（一分钟看懂）

| 平台概念 | 你的服务里对应什么 | 怎么告诉平台 |
|---|---|---|
| 会话（thread） | 一次完整的用户对话 | 每个 span 带同一个会话键 `gen_ai.conversation.id`（例如你的 chat id） |
| 轮次 = 一个 trace | 用户发一条消息、Agent 回一次 | 一轮一个根 span（`gen_ai.operation.name = invoke_agent`） |
| 子 span | 模型调用、工具调用、检索等 | `chat` / `execute_tool` 等子 span |
| Agent 版本 | 你的服务 / Agent 的发布版本 | 每个 span 带 `gen_ai.agent.name`、`gen_ai.agent.version` |
| 组件版本 | Prompt、模型、知识库、工具的版本 | `insightmon.component.<类型>.<名称>.version` |
| 会话结束 | 用户结束对话 | 最后一轮带 `insightmon.thread.end = true`；不带时平台在空闲 10 分钟后自动关闭 |

**没有会话键的 trace 不会进入任何会话**，会话分析、Badcase 都看不到它。这是最常见的接入问题。

---

## 2. 路径 A：Python SDK（推荐）

### 2.1 安装

SDK 仓库是 InsightMon 组织下的私有仓库，安装机器需要有该仓库的读权限（GitHub 账号或 deploy token）：

```bash
pip install "insightmon-eval-sdk @ git+https://github.com/InsightMon/insightmon-eval-sdk.git@main"
```

生产环境把 `@main` 换成固定的 tag 或 commit。导入名是 `insightmon`。

### 2.2 一个普通 Python 服务的完整示例

下面的服务没有用任何 Agent 框架：自己调模型、自己调工具。埋点只有三处：进程启动时 `init()`、每条消息外面包
`thread()` + `turn()`、模型和工具调用各开一个 span。

```python
"""一个不依赖 LangGraph 的普通 Python Agent 服务：手写埋点。"""

import functools

import insightmon

tracer = insightmon.get_tracer()


def traced_tool(fn):
    """装饰器：把一个普通函数记成工具调用 span。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with tracer.start_as_current_span(
            fn.__name__,
            attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": fn.__name__},
        ):
            return fn(*args, **kwargs)

    return wrapper


@traced_tool
def lookup_order(order_id: str) -> dict:
    return {"order_id": order_id, "status": "shipping"}


def call_llm(messages: list[dict]) -> str:
    """调用模型（任何厂商、任何 HTTP 客户端都行）：记成一个 chat span。"""
    with tracer.start_as_current_span(
        "chat gpt-4o",
        attributes={"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o"},
    ) as span:
        answer = "您的订单 A123 正在派送中。"  # 换成真实的模型调用
        span.set_attribute("gen_ai.response.model", "gpt-4o-2024-08-06")
        span.set_attribute("gen_ai.usage.input_tokens", 120)
        span.set_attribute("gen_ai.usage.output_tokens", 18)
        return answer


def handle_message(chat_id: str, user_id: str, text: str, *, last: bool = False) -> str:
    """处理用户的一条消息 = 一轮 = 平台上的一个 trace。"""
    with insightmon.thread(chat_id, user_ref=user_id, metadata={"channel": "app"}):
        with insightmon.turn(end_thread=last):
            order = lookup_order("A123")
            answer = call_llm(
                [{"role": "user", "content": text}, {"role": "tool", "content": str(order)}]
            )
            # 对话内容：只在 init(capture_content=True) 时发送
            insightmon.record_io(input_messages=text, output_messages=answer)
            return answer


if __name__ == "__main__":
    insightmon.init(  # 读取 INSIGHTMON_BASE_URL / INSIGHTMON_API_KEY
        agent_name="order-bot",
        agent_version="2.3.1",  # ← agent 版本，见 §6
        components=[  # ← 组件版本，见 §6
            {"type": "prompt", "name": "system", "version": "p12"},
            {"type": "model", "name": "main", "version": "gpt-4o-2024-08-06"},
        ],
        language="zh",  # zh / en / ja / mixed / und
        environment="production",
        capture_content=True,  # 默认 False：不发送消息内容
    )
    print(handle_message("chat-py-001", "u_9f2", "我的订单到哪了？"))
    print(handle_message("chat-py-001", "u_9f2", "好的谢谢", last=True))
    insightmon.shutdown()  # 短命脚本退出前导出剩余数据；常驻服务退出时自动调用
```

运行后在平台侧栏「会话与 Trace」搜 `chat-py-001`：一个已关闭的会话（关闭原因 `explicit`）、两个 trace，每个
trace 下有 `lookup_order` 工具 span 和 `chat gpt-4o` 模型 span，agent 版本 `2.3.1`，组件 `prompt/system = p12`、
`model/main = gpt-4o-2024-08-06`。

### 2.3 Web 服务里放在哪

- **`init()` 在进程启动时调一次**。gunicorn / uWSGI 等多进程服务在每个 worker 里调（例如 gunicorn 的 `post_fork`
  钩子）。
- **`thread()` + `turn()` 包住处理一条用户消息的那段代码**（例如 FastAPI / Flask 的 handler 内部）。会话键用你自己
  的会话 id，同一会话的每一轮都用同一个。
- **asyncio** 自动带上下文；**线程池**要手动复制上下文：
  `ctx = contextvars.copy_context(); executor.submit(ctx.run, fn, arg)`，否则子线程里的 span 会丢掉会话键。
- 已经在用 OpenTelemetry（例如 `opentelemetry-instrument`、OpenLLMetry、OpenInference 的自动插桩）：`init()` 会接入
  现有的全局 `TracerProvider`，第三方插桩产生的 span 也会自动打上会话键和版本，不需要改动它们。

### 2.4 SDK 常用 API

| 函数 | 作用 |
|---|---|
| `init(agent_name, agent_version, components, language, environment, capture_content, ...)` | 初始化；没有 base URL 和 Key 时只打属性不发送，可以空跑 |
| `thread(key, *, language=None, metadata=None, user_ref=None)` | 会话作用域：块内所有 span 带会话键 |
| `turn(name="turn", *, end_thread=False)` | 一轮对话（一个 trace 的根 span） |
| `end_thread()` | 标记会话结束（等同 `turn(end_thread=True)`） |
| `set_component(type, name, version=...)` | 运行中切换组件版本（在 `thread()` 内只影响该会话） |
| `record_io(system_instructions=, input_messages=, output_messages=)` | 记录对话内容（需 `capture_content=True`） |
| `get_tracer()` | 拿 tracer，用来开模型、工具、检索等子 span |
| `flush()` / `shutdown()` | 立即导出 / 导出并关闭 |

---

## 3. 路径 B：任意语言的 OpenTelemetry SDK

Java、Go、Node.js、.NET 等直接用官方 OpenTelemetry SDK，把 **OTLP/HTTP** 导出器指向平台即可。平台只支持
OTLP over HTTP（protobuf 或 JSON），**不支持 gRPC**。

用标准环境变量配置导出器（各语言 SDK 通用）：

```bash
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="$INSIGHTMON_BASE_URL/otlp/v1/traces"
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
# 请求头里的空格要写成 %20
export OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Bearer%20${INSIGHTMON_API_KEY}"
export OTEL_SERVICE_NAME=order-bot
```

然后在代码里按 [§1.3](#13-数据模型一分钟看懂) 和 [§8](#8-属性速查) 给 span 设置属性。Node.js 示例：

```js
// npm i @opentelemetry/api @opentelemetry/sdk-trace-node @opentelemetry/exporter-trace-otlp-proto
const { NodeTracerProvider, BatchSpanProcessor } = require("@opentelemetry/sdk-trace-node");
const { OTLPTraceExporter } = require("@opentelemetry/exporter-trace-otlp-proto");
const { trace, context } = require("@opentelemetry/api");

const provider = new NodeTracerProvider({
  spanProcessors: [new BatchSpanProcessor(new OTLPTraceExporter({
    url: `${process.env.INSIGHTMON_BASE_URL}/otlp/v1/traces`,
    headers: { Authorization: `Bearer ${process.env.INSIGHTMON_API_KEY}` },
  }))],
});
provider.register();
const tracer = trace.getTracer("order-bot");

// 所有 span 都要带的公共属性
const common = (chatId) => ({
  "gen_ai.conversation.id": chatId,      // 会话键
  "gen_ai.agent.name": "order-bot",
  "gen_ai.agent.version": "2.3.1",       // agent 版本
});

async function handleMessage(chatId, text, last = false) {
  return tracer.startActiveSpan("turn", { attributes: {
    ...common(chatId),
    "gen_ai.operation.name": "invoke_agent",
    "insightmon.component.prompt.system.version": "p12",
    "insightmon.language": "zh",
    ...(last ? { "insightmon.thread.end": true } : {}),
  }}, async (turn) => {
    try {
      await tracer.startActiveSpan("lookup_order", { attributes: {
        ...common(chatId), "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup_order",
      }}, async (s) => { /* 调用工具 */ s.end(); });
      return "您的订单正在派送中。";
    } finally {
      turn.end();
    }
  });
}
```

要点：

- **每个 span 都带会话键**最稳妥（平台会在整条 trace 的所有 span 里找会话键，至少根 span 要带）。
- 不要在 resource 上设 `service.version`，除非它就是 agent 版本：缺少 `gen_ai.agent.version` 时平台会拿它当 agent
  版本。
- 已经有 OpenTelemetry Collector 的团队，也可以在 Collector 里加一个 `otlphttp` 导出器转发：

```yaml
exporters:
  otlphttp/insightmon:
    traces_endpoint: https://insightmon-ai-eval-lulirrtvya-ew.a.run.app/otlp/v1/traces
    logs_endpoint: https://insightmon-ai-eval-lulirrtvya-ew.a.run.app/otlp/v1/logs
    headers:
      Authorization: "Bearer ${env:INSIGHTMON_API_KEY}"
```

---

## 4. 路径 C：直接 HTTP 上报 OTLP/JSON

不想引入任何依赖时，直接 POST OTLP/JSON 到 `/otlp/v1/traces`。要点：

- `Content-Type: application/json`（也接受 `application/x-protobuf`），可选 `Content-Encoding: gzip`；
- `traceId` 是 32 位 hex，`spanId` 是 16 位 hex（随机生成；同一轮的 span 用同一个 `traceId`）；
- 时间是 Unix 纳秒，写成字符串；
- 单个请求解压后 ≤ 8 MiB；平台按 (trace id, span id) 去重，**重试是安全的**。

下面一次请求上报一轮对话：一个轮次根 span + 一个工具子 span，带 agent 版本、组件版本、语言、用户引用、
metadata、对话内容，并关闭会话：

```bash
TRACE_ID=$(openssl rand -hex 16)
ROOT_ID=$(openssl rand -hex 8)
TOOL_ID=$(openssl rand -hex 8)
NOW=$(date +%s%N)   # macOS 自带 date 不支持 %N，可用 python3 -c 'import time;print(time.time_ns())'

cat > turn.json <<EOF
{
  "resourceSpans": [{
    "resource": {"attributes": [
      {"key": "service.name", "value": {"stringValue": "order-bot"}},
      {"key": "deployment.environment.name", "value": {"stringValue": "production"}}
    ]},
    "scopeSpans": [{
      "scope": {"name": "order-bot"},
      "spans": [
        {
          "traceId": "$TRACE_ID", "spanId": "$ROOT_ID", "name": "turn", "kind": 2,
          "startTimeUnixNano": "$NOW", "endTimeUnixNano": "$((NOW + 1500000000))",
          "attributes": [
            {"key": "gen_ai.conversation.id", "value": {"stringValue": "chat-curl-001"}},
            {"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
            {"key": "gen_ai.agent.name", "value": {"stringValue": "order-bot"}},
            {"key": "gen_ai.agent.version", "value": {"stringValue": "2.3.1"}},
            {"key": "insightmon.component.prompt.system.version", "value": {"stringValue": "p12"}},
            {"key": "insightmon.component.model.main.version", "value": {"stringValue": "gpt-4o-2024-08-06"}},
            {"key": "insightmon.language", "value": {"stringValue": "zh"}},
            {"key": "enduser.id", "value": {"stringValue": "u_9f2"}},
            {"key": "insightmon.metadata.channel", "value": {"stringValue": "app"}},
            {"key": "gen_ai.input.messages", "value": {"stringValue": "[{\"role\":\"user\",\"parts\":[{\"type\":\"text\",\"content\":\"我的订单到哪了？\"}]}]"}},
            {"key": "gen_ai.output.messages", "value": {"stringValue": "[{\"role\":\"assistant\",\"parts\":[{\"type\":\"text\",\"content\":\"您的订单已在派送中。\"}]}]"}},
            {"key": "insightmon.thread.end", "value": {"boolValue": true}}
          ],
          "status": {"code": 1}
        },
        {
          "traceId": "$TRACE_ID", "spanId": "$TOOL_ID", "parentSpanId": "$ROOT_ID",
          "name": "lookup_order", "kind": 1,
          "startTimeUnixNano": "$((NOW + 200000000))", "endTimeUnixNano": "$((NOW + 600000000))",
          "attributes": [
            {"key": "gen_ai.conversation.id", "value": {"stringValue": "chat-curl-001"}},
            {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}},
            {"key": "gen_ai.tool.name", "value": {"stringValue": "lookup_order"}},
            {"key": "gen_ai.tool.call.arguments", "value": {"stringValue": "{\"order_id\":\"A123\"}"}},
            {"key": "gen_ai.tool.call.result", "value": {"stringValue": "{\"status\":\"shipping\"}"}}
          ]
        }
      ]
    }]
  }]
}
EOF

curl -sS -X POST "$INSIGHTMON_BASE_URL/otlp/v1/traces" \
  -H "Authorization: Bearer $INSIGHTMON_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @turn.json
# 全部接受时返回 {}；有被拒的 span 时返回 {"partialSuccess": {"rejectedSpans": N, "errorMessage": "..."}}
```

- 多轮对话：每一轮新的 `traceId`，同一个 `gen_ai.conversation.id`；只有最后一轮带 `insightmon.thread.end`。
- 不想发对话内容：去掉 `gen_ai.input.messages` / `gen_ai.output.messages` / `gen_ai.tool.call.*` 即可。
- 返回 200 不代表全部接受：请检查响应里的 `partialSuccess`。

---

## 5. 路径 D：整段会话导入

会话已经结束、手上是完整的聊天记录时（例如每晚从数据库导出、补录历史数据），用一个 REST 请求导入，不需要构造
span。Key 需要 `ingest` 权限。

```bash
curl -sS -X POST "$INSIGHTMON_BASE_URL/api/v1/threads:import" \
  -H "Authorization: Bearer $INSIGHTMON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"threads": [{
    "thread_key": "chat-import-001",
    "agent_name": "order-bot",
    "agent_version": "2.3.1",
    "environment": "production",
    "user_ref": "u_9f2",
    "language": "zh",
    "components": [{"type": "prompt", "name": "system", "version": "p12"}],
    "metadata": {"channel": "app"},
    "turns": [{
      "started_at": "2026-10-28T08:00:00Z",
      "ended_at": "2026-10-28T08:00:03Z",
      "input":  {"messages": [{"role": "user", "content": "我的订单到哪了？"}]},
      "output": {"messages": [{"role": "assistant", "content": "您的订单已在派送中。"}]},
      "model": "gpt-4o",
      "tool_calls": [{"name": "lookup_order", "arguments": {"order_id": "A123"},
                      "result": {"status": "shipping"},
                      "started_at": "2026-10-28T08:00:01Z", "ended_at": "2026-10-28T08:00:02Z"}]
    }]
  }]}'
# → {"items":[{"index":0,"thread_key":"chat-import-001","thread_id":"...","created":true,"trace_ids":["..."]}]}
```

- 一次 1–100 个会话，每个 1–500 轮，请求体 ≤ 8 MiB；**任何一条出错整批不写**（400 `thread_import_invalid`，
  `params` 指出字段和下标）。
- 导入的会话**立即关闭**（关闭原因 `import`），之后不能再追加轮次。
- 幂等：同一个 `thread_key` 内容相同再导入返回 `created: false`；内容不同、或已有同 key 的 OTLP 会话 → 409
  `thread_import_conflict`。所以**同一个会话不要既走 OTLP 又走导入**。
- 每轮的 `agent_version` / `components` 不填时取会话级字段；某几轮版本不同就在那几轮单独填。

---

## 6. Agent 版本与组件版本上报

版本是平台做回归验证、发布观察、按版本对比的依据，**请务必上报**。

| 上报什么 | Python SDK | OTel 属性（路径 B / C） | 会话导入（路径 D） |
|---|---|---|---|
| Agent 名称 | `init(agent_name=)` | `gen_ai.agent.name` | `agent_name` |
| Agent 版本 | `init(agent_version=)` | `gen_ai.agent.version` | `agent_version`（会话级或轮次级） |
| 组件版本 | `init(components=[...])` / `set_component()` | `insightmon.component.<type>.<name>.version` | `components: [{type, name, version}]` |
| 组件内容哈希（可选） | `content_hash=` | `insightmon.component.<type>.<name>.content_hash` | `content_hash` |
| 环境 | `init(environment=)` | resource 上的 `deployment.environment.name` | `environment` |

建议：

- **Agent 版本**用你的发布版本号或 git commit（例如 `2.3.1`、`2.3.1+a1b2c3d`），从构建产物或环境变量读取，不要写死。
  ≤ 200 字符，首尾空白会被去掉。
- **组件**类型默认有 `agent`、`prompt`、`skill`、`knowledge`、`tool`、`model`；名称自定。例如 Prompt 改了就把
  `prompt/system` 的版本从 `p12` 改成 `p13`，平台就能按组件定位是哪次改动引入了问题。
- 灰度 / A-B 实验时，同一进程里不同会话用不同版本：在 `thread()` 里调用 `set_component(...)`，只影响这个会话。
- **回放回执（可选，进阶）**：如果你希望平台调用你的服务做回归回放（HTTP Target），在服务里加
  `insightmon.InsightMonASGIMiddleware` / `InsightMonWSGIMiddleware`，并可以在响应体里带
  `"receipt": insightmon.receipt()`，平台据此确认“实际被测的是哪个版本”。细节见 SDK README 的「回放回执」。

---

## 7. 上报用户反馈与业务结果（可选）

点赞点踩、问题是否解决等，用 `scores:batch` 写到会话上（Key 需要 `score_write`）。名称必须是项目里**已发布的评分
定义**（侧栏「评分定义」先建好，例如 `thumbs_up`，类型 boolean，层级勾选会话；发布需要主审权限）。

```bash
curl -sS -X POST "$INSIGHTMON_BASE_URL/api/v1/scores:batch" \
  -H "Authorization: Bearer $INSIGHTMON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"scores": [{
    "idempotency_key": "fb-chat-py-001-1",
    "name": "thumbs_up",
    "source": "end_user",
    "subject": {"type": "thread", "thread_key": "chat-py-001"},
    "value": false,
    "explanation": "用户点踩"
  }]}'
```

- `source` 只能是 `end_user`（用户反馈）、`outcome`（业务结果）、`import`（外部导入）。
- 主体可以是会话 `{"type":"thread","thread_key":...}`、trace `{"type":"trace","otel_trace_id":...}` 或 span。
- **一定填 `idempotency_key`**，否则重试会重复写入。每批 ≤ 500 条，一条无效整批拒绝。
- Python：`insightmon.Client().scores_batch([...])`，SDK 自动生成幂等键和分批。

---

## 8. 属性速查

| 属性 | 放在 | 必需 | 说明 |
|---|---|---|---|
| `gen_ai.conversation.id` | span | **是** | 会话键，≤ 200 字符；也认 `session.id`、`insightmon.thread_id` |
| `gen_ai.agent.name` | span | 建议 | 缺失时取 resource 的 `service.name` |
| `gen_ai.agent.version` | span | 建议 | 缺失时取 resource 的 `service.version` |
| `gen_ai.operation.name` | span | 建议 | `invoke_agent`（轮次）、`chat`（模型）、`execute_tool`（工具）、`embeddings` 等 |
| `insightmon.component.<type>.<name>.version` | 根 span | 建议 | 组件版本 |
| `insightmon.thread.end` | 最后一轮的 span | 建议 | `true` 时立即关闭会话；不要放在 resource 上 |
| `insightmon.language` | span | 可选 | `zh` / `en` / `ja` / `mixed` / `und`；不填时平台自动识别 |
| `enduser.id` | span | 可选 | 不透明的用户引用，≤ 128 字符，**不要放手机号、邮箱等个人信息** |
| `insightmon.metadata.<key>` | span | 可选 | 写入会话 metadata（渠道、租户等，用于筛选） |
| `deployment.environment.name` | resource | 可选 | 环境，≤ 100 字符 |
| `gen_ai.request.model` / `gen_ai.response.model` | 模型 span | 可选 | 模型名 |
| `gen_ai.usage.input_tokens` / `output_tokens` | 模型 span | 可选 | token 数 |
| `gen_ai.tool.name` / `gen_ai.tool.call.id` | 工具 span | 可选 | 工具名 / 调用 id |
| `gen_ai.input.messages` / `gen_ai.output.messages` | 轮次或模型 span | 可选（内容） | JSON 字符串：`[{"role":"user","parts":[{"type":"text","content":"..."}]}]`，也接受 `{role, content}` |
| `gen_ai.system_instructions` | 同上 | 可选（内容） | 系统提示 |
| `gen_ai.tool.call.arguments` / `gen_ai.tool.call.result` | 工具 span | 可选（内容） | JSON 字符串 |
| `insightmon.cost` | span | 可选 | 成本（double） |

完整列表：SDK 仓库 `docs/conventions.md`。

---

## 9. 内容与隐私

- Python SDK **默认不发送对话内容**（`capture_content=False`），只发结构、版本、耗时、token 等元数据。要在平台上
  看对话、做 LLM 评审，需显式开启 `capture_content=True` 并用 `record_io()` 记录。
- 路径 B / C / D 你发什么平台就收什么：不想上传内容就别填内容字段。
- 平台侧还有一道开关：项目设置的内容采集模式选「仅元数据」时，平台不保存消息内容；平台入库时按项目规则脱敏。
- 会话键、`user_ref`、metadata 里**不要放个人信息**。

---

## 10. 故障排查

| 现象 | 原因与处理 |
|---|---|
| 平台上什么都没有 | Python：确认 `INSIGHTMON_BASE_URL` 和 `INSIGHTMON_API_KEY` 都设置了；短脚本退出前调用 `insightmon.shutdown()`；打开 `logging.getLogger("insightmon").setLevel(logging.DEBUG)` |
| 有 trace 但没进会话 | span 没带 `gen_ai.conversation.id`；Python 线程池里丢了上下文（见 §2.3） |
| 会话一直是进行中 | 最后一轮没带 `insightmon.thread.end`；平台会在 10 分钟无新轮次后自动关闭 |
| 401 / 403 | Key 被吊销、属于别的项目或缺少权限；用 `GET /api/v1/api-key` 查看 |
| 415 `otlp_content_type_unsupported` | `Content-Type` 不是 `application/json` / `application/x-protobuf`；或用了 gRPC |
| 413 `payload_too_large` | 单请求 > 8 MiB，拆小批次 |
| 返回 200 但带 `partialSuccess` | 部分 span 被拒，原因在 `errorMessage` |
| SDK 日志里有 `HTTP 301 (redirect ...)` | base URL 写错（http/https、多了路径）；SDK 不跟随重定向，改成最终地址 |
| 409 `thread_import_conflict` | 同一 `thread_key` 已导入过不同内容，或已有 OTLP 会话 |
| 版本显示为空 | span 上没有 `gen_ai.agent.version`（且 resource 没有 `service.version`） |
