# 主动陪伴 · astrbot_plugin_proactive_companion

让 Bot 在**私聊和群聊中主动发起消息**的 AstrBot 插件。

它不是定时刷屏机器人，而是有"记忆、有脾气、有分寸"的伙伴：会记住你说过什么，
会因为你的语气变开心或低落，会看时间段办事，也能在你明确不想被打扰时闭嘴。

## ✨ 功能

| 能力 | 说明 |
| --- | --- |
| 🗣️ 主动消息 | 定时检查会话，在满足条件时主动开口，私聊与群聊都支持 |
| 🧠 上下文感知 | 记录每个会话的近期对话，并结合 AstrBot 自身的会话历史生成话题 |
| 💾 持久化数据 | 会话、消息、情绪、事件日志全部落盘到 `data/plugin_data/`，重装插件不丢数据 |
| 🎭 动态情绪 | valence（愉悦度）/ arousal（唤醒度）双维度情绪，随互动漂移、随时间衰减，并影响语气与发言频率 |
| 🌙 免打扰时段 | 支持 `23:00-07:30` 这类跨午夜区间，静默期间绝不打扰 |
| 🔊 TTS 集成 | 可选用 AstrBot 的 TTS Provider，把主动消息转成语音 |
| 🖥️ 独立 WebUI | 插件自带可视化面板，管理会话、情绪、配置与日志 |

## 💬 主动回复

除了「定时主动搭话」，插件还能在**机器人本来不会回复**的消息上主动接话——比如群里
有人随口说了一句话，Bot 按概率决定要不要冒个泡。

它不自己生成内容，而是把消息交回 AstrBot 的正常 LLM 链路，所以语气、人设、上下文
和被 @ 时完全一样。

触发条件（全部满足才会接话）：

1. `proactive_reply_enabled` 已开启；
2. 消息**不会**唤醒机器人（群里没被 @、私聊需要唤醒词等）；
3. 消息不是指令（`/` 开头或命中唤醒词），避免和指令回复撞车；
4. 长度不小于 `proactive_reply_min_length`；
5. 不在免打扰时段内；
6. 发送者 ID / 群号 / 会话 ID 命中 `proactive_reply_whitelist`；
7. 距上次主动回复超过 `proactive_reply_cooldown_minutes`；
8. 通过 `proactive_reply_probability` 的概率判定。

白名单留空表示**不允许任何人**主动回复（白名单语义）；填 `*` 表示不限制。

## 📦 安装

在 AstrBot WebUI 的 **插件市场** 中搜索 `主动陪伴` / `proactive_companion` 安装，
或手动克隆到 `data/plugins/` 目录下：

```bash
cd AstrBot/data/plugins
git clone https://github.com/hongdougao111/astrbot_plugin_proactive_companion.git
```

然后在 WebUI 的插件页点击 **重载**。本插件**零第三方依赖**，只用 Python 标准库。

## 💬 指令

| 指令 | 说明 |
| --- | --- |
| `/陪伴 开启` | 为当前会话开启主动消息 |
| `/陪伴 关闭` | 为当前会话关闭主动消息 |
| `/陪伴 状态` | 查看当前会话的开关、情绪、已发送次数与时段状态 |
| `/陪伴 说说` | 立刻触发一条主动消息 |
| `/陪伴 清空` | 清空当前会话的记忆与情绪 |

> 也可使用英文别名：`/companion on|off|status|talk|clear`。

## ⚙️ 配置

所有配置都可以在 **插件配置面板** 或 **插件可视化视图 → 配置** 页修改：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 插件总开关 |
| `check_interval_seconds` | `60` | 主动消息检查间隔（秒，最小 15） |
| `quiet_hours` | `[]` | 免打扰时段，如 `["23:00-07:30"]` |
| `min_idle_minutes` | `45` | 对方至少静默多久才主动开口 |
| `cooldown_minutes` | `180` | 同一会话两次主动发言的最小间隔 |
| `max_daily_messages` | `6` | 每会话每日上限（0 = 不限） |
| `max_proactive_per_tick` | `3` | 单轮检查最多发几条，避免一次性触发大量 LLM 请求 |
| `idle_jitter_ratio` | `0.2` | 静默阈值随机抖动比例，避免所有会话同时被触发 |
| `proactive_reply_enabled` | `false` | 启用主动回复 |
| `proactive_reply_probability` | `0.2` | 主动回复概率（0.0–1.0），0 = 永不，1 = 总是 |
| `proactive_reply_whitelist` | `[]` | 主动回复白名单（用户 ID / 群号 / 会话 ID；`*` = 不限制；留空 = 不允许任何人） |
| `proactive_reply_cooldown_minutes` | `10` | 同一会话两次主动回复的最小间隔 |
| `proactive_reply_min_length` | `2` | 触发主动回复的最短消息长度 |
| `group_enabled` / `private_enabled` | `true` | 是否允许在群聊 / 私聊主动发言 |
| `mention_only_in_group` | `false` | 群里只有被 @ 过才主动说话 |
| `mood_enabled` | `true` | 是否启用动态情绪 |
| `mood_decay_per_hour` | `0.15` | 情绪回归平静的速度 |
| `tts_enabled` | `false` | 是否把主动消息转成语音 |
| `tts_provider_id` | `""` | 指定 TTS 提供商，留空用会话默认 |
| `system_prompt` | 见配置面板 | 主动消息的人设提示词 |

## 🖥️ 可视化面板

插件在 WebUI 中注册了一个名为 **panel** 的视图（插件详情页 → 可视化视图），包含：

- **概览**：运行状态、免打扰时段、平均情绪、今日发送量、调度器健康度
- **会话**：搜索、单独开关、立即发送、重置情绪、查看消息记录、删除会话
- **配置**：可视化编辑全部运行参数并即时保存
- **日志**：最近事件流，以及一键导出全量 JSON 数据

## 🧩 工作原理

1. **采集**：插件挂在「回复前钩子」上，在机器人每次开口前记录这一轮对话（用户说了什么 +
   机器人回了什么），作为上下文与情绪的输入。
   刻意**不使用** `event_message_type(ALL)` 监听器 —— 任何通过过滤器的 handler 都会把
   消息标记为 wake，那会让 Bot 对群里每条消息都做出回复。
2. **调度**：插件在 AstrBot 加载完成后启动一个后台协程，按 `check_interval_seconds` 轮询所有会话。
3. **判定**：依次检查 —— 全局开关 → 会话开关 → 群/私聊许可 → 免打扰时段 → 每日上限 →
   最小静默时长（带随机抖动）→ 冷却时间 → （群里）是否被 @ 过。
4. **生成**：拼装「最近对话 + AstrBot 会话历史 + 当前情绪 + 时间」提示词，交给当前对话模型生成；
   没有可用模型或调用失败时，自动退化为内置模板文案，保证功能永远可用。
5. **投递**：通过 `context.send_message(unified_msg_origin, chain)` 主动发送；启用 TTS 时附带语音消息，
   语音失败会自动降级为纯文本重发一次。

## 🧪 自检

仓库内置了一套离线自检脚本，用 mock 替换 AstrBot 的 API 后真实跑一遍插件核心逻辑
（调度判定、情绪漂移、TTS、Web API、指令、异常兜底）：

```bash
python tests/test_proactive_companion.py
```

当前结果：**77 项断言全部通过**。

## 📋 更新日志

### v1.0.1

- **新增主动回复**：在机器人本来不会回复的消息上，按「回复概率」+「白名单」主动接话，
  走正常 LLM 链路，语气人设与被 @ 时一致
- 新增配置：`proactive_reply_enabled` / `proactive_reply_probability` /
  `proactive_reply_whitelist` / `proactive_reply_cooldown_minutes` /
  `proactive_reply_min_length`
- WebUI 面板新增主动回复相关配置项与触发次数统计
- 数据库自动迁移：sessions 表新增 `proactive_replies` 列（升级不丢数据）

## ❓ 常见问题

**Q：Bot 一点动静都没有？**
A：按顺序检查：插件总开关 → 会话是否 `/陪伴 开启` → 是否处于免打扰时段 →
`min_idle_minutes` / `cooldown_minutes` 是否设置过大 → 面板「概览」里的最近错误。

**Q：群里会不会刷屏？**
A：默认带有最小静默时长、冷却时间、每日上限三层限制；还可以开启
`mention_only_in_group`，让 Bot 只在你 @ 它之后才开口。

**Q：数据存在哪里？**
A：`data/plugin_data/astrbot_plugin_proactive_companion/proactive.db`，不在插件目录内，
更新或重装插件不会丢失。

## 📄 License

[MIT](./LICENSE)

## 🙏 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) 及其插件生态
- 插件可视化视图与 bridge SDK 规范（AstrBot 官方文档）
