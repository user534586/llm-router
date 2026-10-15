# LLM Router

本地优先 + 多云端接力兜底的 LLM 路由服务。给 QwenPaw 之类只认 OpenAI 接口的客户端用。

## 架构

```
客户端 (QwenPaw / curl / 任意 OpenAI 客户端)
        │
        ▼
   ┌──────────────────────────────┐
   │  LLM Router  0.0.0.0:8100    │
   │  ├ 聊天内 /route 指令拦截     │
   │  ├ 本地健康探测（后台探活）    │
   │  ├ 熔断（单链路连挂就冷却）    │
   │  ├ 智能路由（估算 token）      │
   │  ├ 多模态强制本地             │
   │  └ 空正文兜底 / 链路前缀       │
   └──────────────────────────────┘
        │
   ① 本地
        │ 成功 → 直接返回
        │ 失败（超时/连不上/5xx/429/401/402/403/超窗）
        ▼
   ② 云端降级链（按 clouds 顺序接力）
      成功即返回（加 [via:名(模型)] 前缀）
        ▼
   ③ 全挂 → 502/503 + 每个目标的失败原因
```

## 走向由谁决定

**由请求里的 `model` 决定，`/route` 只影响 `model=auto` 时。**

| 客户端发的 model | 走向 |
|---|---|
| `auto`（或空） | 按 `/route` 模式，默认 fallback：本地优先，失败降级整条云端链 |
| `local` / 本地真实模型路径 / `local.alias` 里的名字 | 只走本地 |
| `deepseek-v4.1-flash` | 只走 `dmxapi-deepseek`（失败可继续试别的云端，见 `pin_fallback`） |
| `deepseek` / `agnes` / 云端的 name / alias | 同上，按别名点名 |
| 没配置过的其它名字（如 `gpt-4o`） | **400 报错**，message 里列出所有可用名字（见 `unknown_model`） |
| `/route <云端名>` 之后 | 强制只走那条云端（严格，不降级） |

## /route 手动切换

两种用法都行：**直接在聊天里发**，或走 HTTP 接口。

```
/route              → 查当前模式 + 各链路健康 + 前缀模式
/route fallback     → 自动：本地优先，失败降级云端链（默认）
/route local        → 强制本地，不降级
/route cloud        → 强制云端，整条链按序接力
/route deepseek     → 只走匹配这个名字/别名的云端
```

聊天内发 `/route xxx` 会被拦截，**不会真的调用模型**，直接回一段状态文本。

HTTP 方式：

```bash
curl http://127.0.0.1:8100/route                    # 查询
curl -X POST http://127.0.0.1:8100/route \
  -H "Content-Type: application/json" -d "{\"mode\":\"deepseek\"}"
curl "http://127.0.0.1:8100/route?mode=fallback"
```

切过的模式会存进 `mode.state`（`persist_mode=true` 时），重启不回退。

## 文件

| 文件 | 说明 |
|------|------|
| `config.json` | **配置文件**，改这里就行 |
| `router.py` | 路由服务主程序 |
| `mode.state` | 当前路由模式（`persist_mode=true` 时自动生成，删掉=回到 `default_mode`） |
| `start.bat` / `start-bg.bat` / `stop.bat` / `status.bat` | 启动/后台启动/停止/状态 |
| `tests/selftest_router.py` | 主流程自测（假上游，19 项断言，不花钱） |
| `tests/selftest_v2.py` | 新特性自测（前缀模式/兜底/多模态/持久化/轮转，22 项断言） |
| `tests/smoke_real.py` | 真实链路冒烟（真本地 + 真云端，很小 token） |
| `tests/smoke_prefix.py` | 验证云端正文前缀 |
| `logs/router.log` | 运行日志（超过 `log.max_bytes` 自动轮转成 `.1/.2/.3`） |

## 接到 QwenPaw

1. 启动路由服务（双击 `start.bat`，或 `start-bg.bat`）
2. QwenPaw 里模型 provider 的 **Base URL** 改成：
   ```
   http://127.0.0.1:8100/v1
   ```
3. **Model 填 `auto`** —— 这样路由服务自己决定走本地还是哪条云端，
   你在聊天里发 `/route xxx` 就能随时指挥。
   若想直连某条云端，就填那条云端的 model 名（如 `deepseek-v4.1-flash`）或别名（如 `deepseek`）。
4. API Key 随便填（路由服务自己不校验）
5. `config.json` 里 `server.host`：只本机用就 `127.0.0.1`，手机等设备也要连就 `0.0.0.0`

> 说明：服务默认监听 `0.0.0.0` 且**不校验调用方**，仅限内网自用；
> `config.json` 里是明文 API key，别把这个端口暴露到公网。

## 配置说明 (config.json)

```jsonc
{
  "server": { "host": "0.0.0.0", "port": 8100 },

  "local": {
    "base_url": "http://localhost:11434/v1",
    "model": "local",                        // 发给上游的模型名
    "model_full_name": "gguf", // 上层用真实路径点名时也算“本地”
    "alias": ["本地"],                        // 可选：还想用别的名字点名本地就写这里
    "api_key": "sk-no-key-required",
    "input_modalities": ["text", "image", "video"],
    "capabilities": ["completion", "multimodal"],
    "timeout": 180
  },

  "clouds": [
    {
      "name": "dmxapi-deepseek",   // 显示在前缀和 /route 里
      "alias": ["deepseek"],       // 别名：/route deepseek、model=deepseek 都能点名它
      "base_url": "https://www.dmxapi.cn/v1",
      "model": "deepseek-v4.1-flash",
      "api_key": "sk-...",         // 或留空 api_key + 填 api_key_env 走环境变量
      "api_key_env": "",
      "timeout": 180,
      "enabled": true              // false = 临时停用这条
    }
    // 加云端就往数组里追加对象，顺序 = 接力顺序
  ],

  "router": {
    "enabled": true,

    // ---- 前缀 ----
    "cloud_prefix": "[via:{name}({model})] ",   // {name}/{model} 占位符；留空 "" = 不加前缀
    "prefix_mode": "smart",      // every=每次都加 / smart=换链路才加（默认，防污染历史）/ off=不加

    // ---- 兜底与安全 ----
    "unknown_model": "error",    // model 填了没配置过的名字：error=400 报错并列出可用名字 / auto=当自动模式
    "multimodal_force_local": true,  // 带图/视频的请求在自动模式下只走本地，不丢给纯文本云端

    // ---- 空正文兜底 ----
    "empty_content_fallback": "reasoning",  // content 为空但 reasoning_content 有内容时补上；off=关
    "reasoning_fallback_marker": "",        // 补进正文时想加个提示就写，比如 "[思考] "

    // ---- 模式 ----
    "default_mode": "fallback",  // 启动时的初始模式
    "persist_mode": true,        // /route 切过的模式存进 mode.state，重启不回退

    // ---- 降级判定 ----
    "fallback_on": {
      "timeout": true,            // 本地超时 → 降级
      "connection_error": true,   // 连不上 → 降级
      "http_5xx": true,           // 500/502/503/504 → 降级
      "http_429": true,           // 限流 → 降级
      "http_401": true,           // 密钥失效/欠费（401）→ 降级
      "http_402": true,           // 余额不足（402）→ 降级
      "http_403": true,           // 无权访问（403）→ 降级
      "context_overflow": true,   // 超上下文窗口 → 降级
      "http_4xx": false           // 其他 4xx（请求本身有问题）默认不降级
    },

    "pin_fallback": true,         // model=<云端名> 指名时，它挂了是否继续试其它云端
                                  // /route <云端名> 的严格指定不受此项影响

    "chat_route_enabled": true,   // 聊天内 /route 指令
    "chat_route_debug": false,    // true 会把用户原文写进日志（占空间/涉隐私，默认关）

    "local_health": {             // 后台探活本地模型（GET base_url/models）
      "enabled": true,
      "interval": 30,             // 每 30s 探一次
      "cooldown": 60              // 挂了之后 60s 内直接走云端，不白撞
    },

    "circuit_breaker": {          // 熔断
      "enabled": true,
      "threshold": 3,             // 连续失败 3 次
      "cool_down": 120            // 冷却 120s
    },

    "smart_route": {              // 智能路由：估算请求 token
      "enabled": true,
      "local_max_ctx": 64000,     // 超过这个数直接走云端（本地窗口扛不住）
      "bytes_per_token": 4
    },

    "prefer_local": true          // false = 跳过本地，直接用云端链
  },

  "log": {
    "level": "info",
    "file": "logs/router.log",
    "max_bytes": 5242880,         // 超过 5MB 轮转
    "backup_count": 3             // 最多留 3 个历史文件（.1 .2 .3）
  }
}
```

### 常见修改

| 想做什么 | 改哪里 |
|---------|--------|
| 换本地地址/模型 | `local.base_url` / `local.model` |
| 加第二条云端 | 往 `clouds` 数组追加对象 |
| 调整云端接力顺序 | 调换 `clouds` 顺序（前面的先试） |
| 临时停用某条云端 | 它的 `enabled` 设 `false` |
| 改端口 | `server.port` |
| 去掉前缀 | `router.cloud_prefix` 设 `""` |
| 每条回复都要看链路 | `router.prefix_mode` 设 `"every"` |
| 跳过本地只用云端 | `router.prefer_local` 设 `false` |
| 本地一挂就别再试 | `local_health.enabled` 设 `true`（默认） |
| 别把用户原文写日志 | `router.chat_route_debug` 保持 `false` |

改完 `config.json` 后**重启服务**生效。

## 云端前缀

走云端时，正文前面会带上是哪条链路（**真正命中的那条**）在回：

```
[via:dmxapi-deepseek(deepseek-v4.1-flash)] 我是 DeepSeek……
```

- 非流式：加在 `choices[].message.content`
- 流式：**真·边收边发**（chunked 透传），前缀只注入到第一个非空 content 分片上
- **工具调用响应（tool_calls）不加前缀**，不动工具调用协议
- 只走本地时不加（本地就是默认，不需要标）
- `prefix_mode` 决定加不加：
  - `smart`（默认）：对话历史里最后一条前缀和这次相同，就**不再重复加**（避免每轮回复都顶个前缀、把历史污染掉）；换了链路（比如从 agnes 切到 deepseek，或从本地掉到云端）会重新出现
  - `every`：每次都加（想一眼看到每轮走了谁都行）
  - `off`：不加

  > 想知道"这一轮到底走了谁"，用 `smart` 时看**第一次**或**换链路那次**，或者直接看 `X-Router-Source` 响应头。

## 空正文兜底

有些思考模型（本地 Huihui 这类）在 `max_tokens` 偏小时，`content` 会是**空串**，
正文全在 `reasoning_content` 里 —— 客户端看起来就是"回复是空的"。

`empty_content_fallback: "reasoning"` 打开后，路由器会在 content 为空时把
`reasoning_content` 填进 content：

- 非流式：填进 `message.content`
- 流式：在收尾分片之前补一个内容分片
- 工具调用响应、content 本来就有内容的响应：不动
- 触发时日志会有 WARN，响应头 `X-Router-Why` 里也会写 `reasoning_fallback`
- 不想要这个行为就设 `off`（那就只能去调大上游的 `max_tokens`）

## 响应头

每个响应都带，方便排查（中文会做百分号编码）：

| 头 | 例子 | 含义 |
|---|---|---|
| `X-Router-Source` | `cloud[dmxapi-deepseek]` | 这次真正服务你的是谁 |
| `X-Router-Mode` | `fallback` | 当前路由模式 |
| `X-Router-Why` | `...; reasoning_fallback` | 为什么这么走（含 `skip_local` / `est_tokens` / 多模态等） |

```bash
curl -si -X POST http://127.0.0.1:8100/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}" | head -12
```

## 接口

| 接口 | 说明 |
|------|------|
| `POST /v1/chat/completions` | 主接口，`stream=true/false` 都支持 |
| `GET /v1/models` | 列出 local / auto / 各云端模型名 + 所有别名 |
| `GET /health` | 模式 + 前缀模式 + 本地健康 + 每条云端的熔断状态 + 可用 model 名 |
| `GET|POST /route` | 查询/切换路由模式 |

## 快速自测

```bash
python tests\selftest_router.py    # 19 项断言，起假上游，不花钱不联网
python tests\selftest_v2.py        # 22 项断言，覆盖前缀模式/兜底/多模态/持久化/轮转
python tests\smoke_real.py         # 真实本地+云端冒烟
python tests\smoke_prefix.py       # 真实云端正文前缀

curl http://127.0.0.1:8100/health
curl http://127.0.0.1:8100/route
```

## 已知限制

| 限制 | 说明 |
|------|------|
| 流式中途断流无法降级 | 一旦 200 + 首个分片发出去了，上游中途断开就**只能**给客户端一个截断流，没法改走云端。需要完整降级就用 `stream=false`。 |
| 模式之外的运行状态不落盘 | 本地健康 / 熔断状态重启就清零，靠探活（≤30s）自愈 |
| 多模态只保证"不误发云端" | 自动模式下带图请求只走本地；本地不可用时直接 400 报错，而不是把图丢给只吃文本的云端 |
| 智能路由是估算 | `smart_route` 按 `bytes_per_token` 粗估 token，超 `local_max_ctx` 就跳过本地 |

## 排查

| 现象 | 检查 |
|------|------|
| 服务起不来 | `logs/router.log`；端口是否被占用（`status.bat`） |
| 一直走云端 | 本地是否可达（`curl .../health` 看 `local.blocked_for`） |
| 云端 401/402 | `clouds[].api_key` 是否正确、余额是否够（会自动降级到下一条） |
| 全挂 | 502/503 响应体里有 `tried`，每条链路的失败原因都在 |
| 回复是空的 | 看日志有没有 `reasoning_fallback` WARN；有的话说明是上游 `max_tokens` 不够，调大即可 |
| 报 `invalid_model` | model 填了 `config.json` 里没有的名字；报错信息里有可用清单，或看 `GET /health` 的 `models` |
| 不知道走的哪条 | 看正文前缀、`X-Router-Source` 响应头，或日志里的 `plan mode=... plan=[...]` |
| 模式重启后变了 | 检查 `persist_mode`；`mode.state` 存着上次的模式，删掉即回到 `default_mode` |
