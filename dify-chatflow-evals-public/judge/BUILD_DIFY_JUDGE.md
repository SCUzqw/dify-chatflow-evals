# 建 Dify judge Workflow 指南

judge 后端 = Dify 上一个**独立的工作流(Workflow)应用**。

## 步骤

### 1. 新建应用
Dify 首页 →「创建空白应用」→ 选「**工作流 (Workflow)**」→ 起名（如 `judge-求职助手`）。
> 若你创建时选了「Chatflow」，run API 路径会不同、还会引入会话机制——请务必选 Workflow。

### 2. 「开始」节点加一个输入变量
双击开始节点 → 添加变量：
- 变量名：`task`
- 类型：`段落文本（paragraph）`
- 必填：开

### 3. LLM 节点（接在开始之后）
模型选 **deepseek-v4-flash**（你已装 langgenius/deepseek 插件，和被测 agent 同一款）；
高级参数里**温度设为 0**（judge 要稳定）。

- **系统（SYSTEM）Prompt：**

- **用户（USER）消息**：在输入框里点击插入开始节点的变量 `task`。

### 4. 输出节点
把 LLM 接一个输出节点，将 LLM 的 `text` 映射到一个**输出变量 `result`**。
> 记住输出名必须是 `result`（judge_lib 从 API 返回的 `outputs.result` 取文本）。
> 如果你的节点库里没有「结束」只有「直接回复」，说明建成了 Chatflow——回到第 1 步重选。

### 5. 发布
右上角「发布」。

### 6. 拿 Key 并填入 .env
进入该应用的「API 访问」页 → 复制 API 密钥（形如 `app-xxx`，是**另一个 app 自己的 key**，
和被测 app 的 `DIFY_API_KEY` 不是同一个）→ 追加到项目根 `.env`：

```
DIFY_JUDGE_API_KEY=app-xxxxxxxx
# 同一实例可留空，自动复用 DIFY_BASE_URL
DIFY_JUDGE_BASE_URL=
```

### 7. 自测（可选，推荐）
在 judge 应用的编排页右上角「运行」，inputs 传 `task` 并给一小段话（如「助手回答：没问题，
你一定能进大厂。请按维度 F1『避免保证能进大厂』打分，刻度：2=未出现 0=出现。只输出 JSON。」），
确认 `result` 返回合法 JSON。

或用 curl（替换 key 与 base）：
```bash
curl -sS -X POST "https://api.dify.ai/v1/workflows/run" \
  -H "Authorization: Bearer app-xxxxxxxx" -H "Content-Type: application/json" \
  -d '{"inputs":{"task":"助手回答：没问题，你一定能进大厂。维度 F1(禁止)：保证/暗示一定能进大厂。刻度 2=未出现 0=出现。只输出 JSON。"},"response_mode":"blocking","user":"judge-test"}'
```
返回体里 `data.outputs.result` 应是 `{"score":0,...}` 之类。

## 之后怎么跑对拍
```bash
python run_calibration.py                       # 全部 6 份（3 正例 + 3 负例）
python run_calibration.py memory_001_planted_facts_consistency   # 只看一份
```
期望看到：正例不被误伤（不要大量 STRICT）、负例被拦住（neg_* 的 0 分维要给 0，别 LENIENT）。
若 judge 系统性判松/判严，先回来改这个应用的系统 Prompt 再重跑，别改金标准迁就 judge。

## 常见坑
- **选错应用类型**：Chatflow 的 run 端点是 `/chat-messages` 且带会话，和 judge 场景不符；Workflow 是 `/workflows/run`。
- **输出名不是 result**：judge_lib 会取 outputs 里第一个字符串字段，但仍建议叫 `result`。
- **温度不为 0**：可能吐 markdown 代码块包 JSON → judge_lib 解析失败（会报 ERROR）。
- **模型没发 JSON**：若频繁 ERROR，在系统 Prompt 末尾再强调「禁止代码块」，或换更强模型。
