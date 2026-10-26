# Dify Chatflow 评测脚手架（第 1–2 步）

针对用 **Dify Chatflow** 搭的对话 agent（如求职规划小助手）的自动化评估。
当前版本只做**前两步**：固定剧本场景 + 跑通多轮对话、把 transcript 落盘。
LLM-judge 打分与人工校准是下一步（见文末「下一步」）。

原理：**把 Chatflow 当黑盒**，用 HTTP API 逐轮驱动真实对话，多轮连续性靠
Dify 服务端按 `conversation_id` + 同一 `user` 保存上下文。

---

## 快速开始

```bash
cd dify-chatflow-evals

# 1) 装依赖（建议先建虚拟环境）
python -m venv .venv
.venv/Scripts/activate        # Windows
pip install -r requirements.txt

# 2) 配置 .env
cp .env.example .env          # 然后编辑填入 DIFY_BASE_URL / DIFY_API_KEY

# 3) 跑评测
python run_eval.py --markdown
```

### 怎么拿到 Base URL 和 API Key（重点）

Dify 应用默认不对外提供 API，必须走以下步骤：

1. 打开你的 Chatflow 应用 → 点右上角 **「发布」**（**应用必须处于已发布状态**，否则接口会报错）。
2. 进入左侧 **「API 访问」**（部分版本叫「访问 API」）。
3. 页面顶部会显示 **API Base URL**，例如：
   - 云端：`https://api.dify.ai/v1`
   - 自托管：`http://你的域名/v1`
   把**含结尾 `/v1` 的整串**复制进 `.env` 的 `DIFY_BASE_URL`。
4. 在「API 密钥」处 **生成新密钥**（形如 `app-xxxxxxxxxx`），复制进 `DIFY_API_KEY`。
   注意：密钥按应用区分，且是**只用于外部调用**的 Service API Key。

> 每改动一次 Chatflow 流程都要重新**「发布」**；未发布或重新发布后旧
> `conversation_id` 会失效（脚手架会给出明确报错）。

### 端点路径不确定？

`dify_client.py` 会在 `/chat-messages` 与 `/chatflow/chat-messages` 之间**自动回退**，
大多数版本不用手动配置。如果仍报「所有候选端点都不存在」，打开「API 访问」页
的 **curl 样例**，把样例里的消息路径（如 `/chatflow/chat-messages`）填进
`.env` 的 `DIFY_CHAT_ENDPOINT`。

---

## 场景文件（评测集）怎么写

`scenarios/` 目录下的 `.json`，一个文件可含多个场景。字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 全局唯一，用作输出文件名 |
| `persona` | 建议 | 用户画像（给评分人看的上下文） |
| `goal` | 建议 | 这一场对话要达成的目标 |
| `turns` | ✅ | **用户侧固定话术**，非空字符串数组（多轮剧本） |
| `expectations` | 否 | 期望 agent 出现的行为清单 |
| `forbidden` | 否 | 禁止出现的行为清单（如“保证能找到工作”） |
| `note` | 否 | 备注：这个 case 想测什么 |

文件支持三种形态：单个对象、对象数组、或 `{"scenarios": [...]}`。
已内置两个示例：

- `example_career_transfer.json`：转行产品经理（无经验、需澄清）
- `example_memory_edge.json`：跨轮记忆一致性 + 信息不足时的追问节奏

> 真实使用建议：从**真实（脱敏后的）对话日志**里挑高价值片段改写成固定剧本，
> 而不是凭空构造。写场景时 `turns` 是“用户会说什么”，不是“我们希望 agent 怎么答”。

---

## 输出

默认写入 `transcripts/`（已 gitignore），每个场景一个文件：

- `{场景id}.jsonl`
  - 第 1 行：`type:"meta"`（画像/目标/期望/禁止/是否跑通/运行时间）
  - 之后每行：一轮对话，含 `turn/user/assistant/conversation_id/latency_s`
  - 该格式供后续 LLM-judge 直接读取打分
- `{场景id}.md`（加 `--markdown` 才有）：人类可读版，供**人工打分/校准**

命令行选项：`--scenarios 文件或目录`、`--out`、`--repeat N`（每个场景跑 N 次测稳定性）、
`--tag`、`--markdown`、`--env`。

---

## 多轮机制的注意事项

- 同一场对话内，`user` 必须保持不变（脚手架每次运行自动生成随机 user，防互相污染）。
- `conversation_id` 用**接口返回的那个**，不要自己编 UUID。
- 想给流程传会话变量（Dify 里配置过的 start node 变量），把键值放进 `inputs` 即可。
- **确定性**：接口层无法设 temperature，需在 Chatflow 各 LLM 节点里调低
  （回归对比时建议趋近 0），并用 `--repeat` 多次采样看分布。
- **版本记录**：改动流程后建议顺手导出 Chatflow DSL（YAML）放进 git，和评测结果
  对应，否则分数下降时无法归因到是哪版流程。

---

## 下一步（第 3 步：打分与校准）

1. 先手工给 20–30 条 transcript 打分（用 `.md` 文件），按维度写死 rubric 锚点。
2. 再上 LLM-judge：逐维度（一次只评一个维度）用结构化输出打分，和人工分对比一致性。
3. 一致性不足就改 rubric，对齐后再固化为 golden set 与回归基线。

需要的话我可以接着给你写 judge 模块。

---

## 常见错误对照

| 现象 | 原因 / 处理 |
|---|---|
| 缺少 DIFY_BASE_URL / DIFY_API_KEY | 按上面「怎么拿到」配置 .env |
| 鉴权失败（HTTP 401） | key 复制不全，或应用没「发布」 |
| conversation_id 不存在/已失效 | 流程重新发布过，重跑（脚手架每次都是新会话，一般不会碰到） |
| 所有候选端点都不存在 | 看「API 访问」页 curl 样例，设置 DIFY_CHAT_ENDPOINT |
| 中途网络超时 | `.env` 里调大 REQUEST_TIMEOUT |
