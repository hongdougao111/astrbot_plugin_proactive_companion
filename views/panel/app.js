// 主动陪伴 · 可视化面板
// 通过 window.AstrBotPluginView bridge 与插件后端通信。

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  config: {},
  sessions: [],
  events: [],
  currentSession: null,
};

const CONFIG_FIELDS = [
  { key: "enabled", label: "插件总开关", type: "bool" },
  { key: "check_interval_seconds", label: "检查间隔（秒）", type: "int", min: 15 },
  { key: "quiet_hours", label: "免打扰时段", type: "text", list: true, hint: "如 23:00-07:30，多个用英文逗号分隔" },
  { key: "min_idle_minutes", label: "最小静默时长（分钟）", type: "int", min: 0 },
  { key: "cooldown_minutes", label: "两次主动消息间隔（分钟）", type: "int", min: 0 },
  { key: "max_daily_messages", label: "每日主动消息上限（0 不限）", type: "int", min: 0 },
  { key: "max_proactive_per_tick", label: "单轮最多发送数", type: "int", min: 1 },
  { key: "idle_jitter_ratio", label: "静默阈值抖动比例", type: "float", min: 0 },
  { key: "group_enabled", label: "允许群聊主动发言", type: "bool" },
  { key: "private_enabled", label: "允许私聊主动发言", type: "bool" },
  { key: "mention_only_in_group", label: "群聊仅被 @ 后发言", type: "bool" },
  { key: "mood_enabled", label: "启用动态情绪", type: "bool" },
  { key: "mood_decay_per_hour", label: "情绪衰减速度 / 小时", type: "float", min: 0 },
  { key: "mood_affect_frequency", label: "情绪影响发言频率", type: "bool" },
  { key: "tts_enabled", label: "启用 TTS 语音", type: "bool" },
  { key: "tts_provider_id", label: "TTS 提供商 ID", type: "text", hint: "留空则使用会话默认的 TTS 提供商" },
  { key: "tts_send_text", label: "语音同时发送文本", type: "bool" },
  { key: "tts_max_chars", label: "TTS 文本长度上限", type: "int", min: 0 },
  { key: "max_context_messages", label: "上下文消息条数", type: "int", min: 1 },
  { key: "keep_messages_per_session", label: "每会话保留消息数", type: "int", min: 10 },
  { key: "keep_events", label: "保留事件日志条数", type: "int", min: 50 },
  { key: "proactive_reply_enabled", label: "启用主动回复", type: "bool" },
  { key: "proactive_reply_probability", label: "主动回复概率 (0.0-1.0)", type: "float", min: 0 },
  { key: "proactive_reply_whitelist", label: "主动回复白名单", type: "text", list: true, hint: "用户 ID / 群号 / 会话 ID，逗号分隔；留空 = 不限制；* = 不限制" },
  { key: "proactive_reply_cooldown_minutes", label: "主动回复冷却(分钟)", type: "int", min: 0 },
  { key: "proactive_reply_min_length", label: "最短触发消息长度", type: "int", min: 0 },
  { key: "capture_context_enabled", label: "采集对话上下文", type: "bool" },
  { key: "proactive_reply_context_messages", label: "注入上下文条数", type: "int", min: 1 },
  { key: "capture_context_max_sessions", label: "最多采集会话数", type: "int", min: 1 },
  { key: "atmosphere_enabled", label: "启用读空气", type: "bool" },
  { key: "atmosphere_window_minutes", label: "气氛判断窗口(分钟)", type: "int", min: 1 },
  { key: "atmosphere_refresh_minutes", label: "气氛刷新间隔(分钟)", type: "int", min: 1 },
  { key: "atmosphere_cold_rate", label: "冷清阈值(条/分钟)", type: "float", min: 0 },
  { key: "atmosphere_hot_rate", label: "热闹阈值(条/分钟)", type: "float", min: 0 },
  { key: "atmosphere_conflict_ratio", label: "紧张阈值(冲突占比)", type: "float", min: 0 },
  { key: "atmosphere_boost", label: "冷场概率放大倍数", type: "float", min: 0 },
  { key: "affection_enabled", label: "启用好感度", type: "bool" },
  { key: "system_prompt", label: "人设提示词", type: "textarea" },
];

function toast(message, isError = false) {
  const el = $("#toast");
  if (!el) return;
  el.textContent = message;
  el.hidden = false;
  el.style.background = isError ? "var(--bad)" : "var(--text)";
  el.style.color = isError ? "#fff" : "var(--panel)";
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    el.hidden = true;
  }, 2600);
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function esc(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[c]);
}

// ---------------------------------------------------------------- 概览
async function loadState() {
  const data = await bridge.apiGet("state");
  state.overview = data;

  const cards = [
    { num: data.sessions, label: "会话总数" },
    { num: data.active_sessions, label: "已开启会话" },
    { num: data.sent_today, label: "今日已发送" },
    { num: data.proactive_replies_total ?? 0, label: "主动回复次数" },
    { num: data.in_quiet_hours ? "免打扰" : "可打扰", label: "时段状态" },
  ];
  $("#stat-cards").innerHTML = cards
    .map((c) => `<div class="card stat"><div class="num">${esc(c.num)}</div><div class="label">${esc(c.label)}</div></div>`)
    .join("");

  const skipReasons = Object.entries(data.proactive_reply_skip_reasons || {})
    .map(([k, v]) => `${k}×${v}`)
    .join("、");
  const detail = [
    ["插件状态", data.enabled ? "运行中" : "已停用"],
    ["调度器", data.running ? "在线" : "未启动"],
    ["检查间隔", `${data.interval} 秒`],
    ["免打扰时段", (data.quiet_hours || []).join("、") || "未设置"],
    ["下一次可打扰", data.next_available || "现在"],
    ["情绪标签", data.mood_label],
    ["主动回复", data.proactive_reply_enabled ? "已开启" : "未开启"],
    [
      "回复概率",
      `${data.proactive_reply_probability ?? 0}（白名单：${
        (data.proactive_reply_whitelist || []).join("、") || "空 = 不限制"
      }）`,
    ],
    ["最近判定", data.proactive_reply_last_decision || "—"],
    ["跳过原因 TOP", skipReasons || "—"],
    [
      "上下文采集",
      `${data.capture_context_enabled ? "开" : "关"}（已跟踪 ${
        data.captured_sessions ?? 0
      } 个会话）`,
    ],
    [
      "读空气",
      `${data.atmosphere_enabled ? "开" : "关"}（${
        Object.entries(data.atmosphere_levels || {})
          .map(([k, v]) => `${k}×${v}`)
          .join("、") || "暂无数据"
      }）`,
    ],
    ["TTS", data.tts_enabled ? "已启用" : "未启用"],
    ["上次检查", fmtTime(data.last_tick_at)],
    ["最近错误", data.last_tick_error || "无"],
    ["数据目录", data.data_dir || ""],
  ];
  $("#state-detail").innerHTML = detail
    .map(([k, v]) => `<div class="row"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`)
    .join("");

  const valence = Math.max(0, Math.min(1, (Number(data.avg_valence) + 1) / 2));
  const arousal = Math.max(0, Math.min(1, Number(data.avg_arousal)));
  $("#mood-valence").style.width = `${(valence * 100).toFixed(1)}%`;
  $("#mood-arousal").style.width = `${(arousal * 100).toFixed(1)}%`;
  $("#mood-label").textContent = data.mood_label || "平静";

  const dot = $("#run-dot");
  dot.className = `dot ${data.enabled && data.running ? "on" : "off"}`;
  $("#subtitle").textContent = `${data.sessions} 个会话 · 今日 ${data.sent_today} 条 · ${data.in_quiet_hours ? "免打扰时段内" : "可自由发言"}`;
}

// ---------------------------------------------------------------- 会话
async function loadSessions() {
  const q = ($("#session-search")?.value || "").trim();
  const data = await bridge.apiGet("sessions", q ? { q } : undefined);
  state.sessions = data.sessions || [];
  $("#session-count").textContent = `共 ${data.total ?? state.sessions.length} 个会话`;

  const rows = state.sessions
    .map((s) => {
      const chat = s.chat_type === "group" ? "群聊" : "私聊";
      const moodClass = s.valence >= 0.15 ? "good" : s.valence <= -0.15 ? "bad" : "";
      const air = s.atmosphere || {};
      const airClass =
        s.atmosphere_level === "热闹"
          ? "good"
          : s.atmosphere_level === "紧张"
            ? "bad"
            : "";
      const airText = s.atmosphere_level
        ? `${s.atmosphere_level}${
            air.message_count
              ? ` · ${air.message_count}条/${air.participants}人`
              : ""
          }`
        : "—";
      return `<tr data-key="${esc(s.session_key)}">
        <td class="name" title="${esc(s.title || s.session_key)}">${esc(s.title || s.session_key)}</td>
        <td>${esc(chat)}${s.mentioned ? " <span class=\"badge\">@过我</span>" : ""}</td>
        <td><span class="badge ${moodClass}">${esc(s.mood_label)}</span></td>
        <td><span class="badge ${airClass}" title="${esc(air.advice || "")}">${esc(airText)}</span></td>
        <td>${esc(s.messages_sent)}</td>
        <td>${esc(s.daily_count)}</td>
        <td><span class="badge ${s.enabled ? "good" : "bad"}">${s.enabled ? "开启" : "关闭"}</span></td>
        <td>
          <div class="row-actions">
            <button class="btn tiny ghost" data-act="toggle">${s.enabled ? "关闭" : "开启"}</button>
            <button class="btn tiny ghost" data-act="send">立即发送</button>
            <button class="btn tiny ghost" data-act="mood">重置情绪</button>
            <button class="btn tiny ghost" data-act="messages">记录</button>
            <button class="btn tiny ghost danger" data-act="delete">删除</button>
          </div>
        </td>
      </tr>`;
    })
    .join("");
  $("#session-rows").innerHTML =
    rows || `<tr><td colspan="8" class="muted">暂无会话记录</td></tr>`;
}

async function sessionAction(key, act, btn) {
  const original = btn.textContent;
  btn.disabled = true;
  try {
    if (act === "toggle") {
      await bridge.apiPost("sessions/toggle", { session_key: key });
      toast("已更新会话状态");
    } else if (act === "send") {
      const r = await bridge.apiPost("sessions/send", { session_key: key });
      toast(r.sent ? `已发送：${(r.text || "").slice(0, 30)}` : "发送失败或无内容", !r.sent);
    } else if (act === "mood") {
      await bridge.apiPost("sessions/reset_mood", { session_key: key });
      toast("情绪已重置");
    } else if (act === "messages") {
      await loadMessages(key);
      return;
    } else if (act === "delete") {
      await bridge.apiPost("sessions/delete", { session_key: key });
      toast("会话数据已删除");
    }
    await Promise.all([loadSessions(), loadState()]);
  } catch (err) {
    toast(err.message || "操作失败", true);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

async function loadMessages(key) {
  const data = await bridge.apiGet("sessions/messages", { session_key: key, limit: 80 });
  state.currentSession = key;
  $("#message-card").hidden = false;
  const items = (data.messages || [])
    .map(
      (m) => `<div class="msg ${esc(m.role)}">
        <div class="meta">${esc(m.role)} · ${esc(m.sender || "")}${
        m.sender_id ? ` (${esc(m.sender_id)})` : ""
      } · ${fmtTime(m.ts)}</div>
        <div>${esc(m.content)}</div>
      </div>`
    )
    .join("");
  $("#message-list").innerHTML = items || `<div class="muted">暂无消息</div>`;
  $("#message-list").scrollTop = $("#message-list").scrollHeight;
}

// ---------------------------------------------------------------- 画像
let editingProfile = null;

async function loadProfiles() {
  const data = await bridge.apiGet("profiles");
  state.profiles = data.profiles || [];
  $("#profile-count").textContent = `共 ${data.total ?? state.profiles.length} 份画像`;
  $("#profile-rows").innerHTML =
    state.profiles
      .map((p) => {
        const idx = p.level_index ?? 0;
        const bar = "█".repeat(idx) + "░".repeat(4 - idx);
        return `<tr data-key="${esc(p.session_key)}" data-user="${esc(p.user_id)}">
        <td>${esc(p.nickname || "—")}</td>
        <td class="name">${esc(p.user_id)}</td>
        <td><span class="badge ${p.affection >= 30 ? "good" : ""}" title="${esc(p.level)}">${esc(
      p.level
    )} ${p.affection.toFixed(1)} <span class="muted">${bar}</span></span></td>
        <td>${esc(p.msg_count)} / ${esc(p.bot_replies)}</td>
        <td>${esc((p.tags || []).join("、") || "—")}</td>
        <td class="name" title="${esc(p.note || "")}">${esc(p.note || "—")}</td>
        <td><button class="btn tiny ghost" data-act="edit-profile">编辑</button></td>
      </tr>`;
      })
      .join("") || `<tr><td colspan="7" class="muted">还没有画像，聊几句就会出现</td></tr>`;
}

function openProfileEditor(profile) {
  editingProfile = profile;
  $("#profile-edit-card").hidden = false;
  $("#pe-nickname").value = profile.nickname || "";
  $("#pe-tags").value = (profile.tags || []).join(", ");
  $("#pe-affection").value = profile.affection ?? 0;
  $("#pe-note").value = profile.note || "";
  $("#profile-edit-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function saveProfile() {
  if (!editingProfile) return;
  const btn = $("#btn-save-profile");
  btn.disabled = true;
  try {
    await bridge.apiPost("profiles/update", {
      session_key: editingProfile.session_key,
      user_id: editingProfile.user_id,
      nickname: $("#pe-nickname").value,
      tags: $("#pe-tags").value,
      affection: Number($("#pe-affection").value),
      note: $("#pe-note").value,
    });
    toast("画像已保存");
    await loadProfiles();
  } catch (err) {
    toast(err.message || "保存失败", true);
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------- 配置
async function loadConfig() {
  const data = await bridge.apiGet("config");
  state.config = data.config || {};
  const form = $("#config-form");
  form.innerHTML = CONFIG_FIELDS.map((f) => {
    const value = state.config[f.key];
    const id = `cfg-${f.key}`;
    let control;
    if (f.type === "bool") {
      control = `<div class="switch"><input type="checkbox" id="${id}" data-key="${f.key}" data-type="bool" ${value ? "checked" : ""} /><span>${value ? "已开启" : "已关闭"}</span></div>`;
    } else if (f.type === "textarea") {
      control = `<textarea id="${id}" data-key="${f.key}" data-type="text">${esc(value ?? "")}</textarea>`;
    } else if (f.list) {
      const arr = Array.isArray(value) ? value.join(", ") : value ?? "";
      control = `<input type="text" id="${id}" data-key="${f.key}" data-type="list" value="${esc(arr)}" />`;
    } else {
      control = `<input type="${f.type === "int" || f.type === "float" ? "number" : "text"}" id="${id}" data-key="${f.key}" data-type="${f.type}" ${f.min !== undefined ? `min="${f.min}"` : ""} value="${esc(value ?? "")}" />`;
    }
    return `<div class="field">
      <label for="${id}">${esc(f.label)}</label>
      ${control}
      ${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ""}
    </div>`;
  }).join("");

  form.querySelectorAll('input[type="checkbox"]').forEach((input) => {
    input.addEventListener("change", () => {
      const span = input.parentElement.querySelector("span");
      if (span) span.textContent = input.checked ? "已开启" : "已关闭";
    });
  });
}

function collectConfig() {
  const out = {};
  $$("#config-form [data-key]").forEach((el) => {
    const key = el.dataset.key;
    const type = el.dataset.type;
    if (type === "bool") {
      out[key] = !!el.checked;
    } else if (type === "int") {
      out[key] = parseInt(el.value || "0", 10) || 0;
    } else if (type === "float") {
      out[key] = parseFloat(el.value || "0") || 0;
    } else if (type === "list") {
      out[key] = String(el.value || "")
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
    } else {
      out[key] = el.value ?? "";
    }
  });
  return out;
}

async function saveConfig() {
  const btn = $("#btn-save-config");
  btn.disabled = true;
  try {
    const payload = collectConfig();
    await bridge.apiPost("config/save", { config: payload });
    $("#config-tip").textContent = `已保存 ${new Date().toLocaleTimeString()}`;
    toast("配置已保存");
    await loadState();
  } catch (err) {
    toast(err.message || "保存失败", true);
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------- 日志
async function loadEvents() {
  const data = await bridge.apiGet("events", { limit: 200 });
  state.events = data.events || [];
  $("#log-count").textContent = `最近 ${state.events.length} 条事件`;
  $("#log-list").innerHTML = state.events
    .map(
      (e) => `<div class="log-item">
        <span class="time">${fmtTime(e.ts)}</span>
        <span class="kind">${esc(e.kind)}</span>
        <span class="payload">${esc(e.payload || "")}</span>
      </div>`
    )
    .join("") || `<div class="log-item"><span class="muted">暂无事件</span></div>`;
}

// ---------------------------------------------------------------- 初始化
function bindTabs() {
  $$("#tabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$("#tabs .tab").forEach((t) => t.classList.remove("active"));
      $$(".panel").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      const panel = $(`#tab-${tab.dataset.tab}`);
      if (panel) panel.classList.add("active");
      if (tab.dataset.tab === "sessions") loadSessions().catch((e) => toast(e.message, true));
      if (tab.dataset.tab === "config") loadConfig().catch((e) => toast(e.message, true));
      if (tab.dataset.tab === "profiles")
        loadProfiles().catch((e) => toast(e.message, true));
      if (tab.dataset.tab === "logs") loadEvents().catch((e) => toast(e.message, true));
    });
  });
}

function bindActions() {
  $("#btn-refresh").addEventListener("click", async () => {
    try {
      await Promise.all([loadState(), loadSessions(), loadEvents(), loadProfiles()]);
      toast("已刷新");
    } catch (err) {
      toast(err.message || "刷新失败", true);
    }
  });

  $("#btn-tick").addEventListener("click", async () => {
    toast("调度器将在下一个周期自动检查");
  });

  $("#btn-save-config").addEventListener("click", saveConfig);

  $("#btn-close-msgs").addEventListener("click", () => {
    $("#message-card").hidden = true;
    state.currentSession = null;
  });

  $("#btn-export").addEventListener("click", async () => {
    try {
      await bridge.download("export", {}, "proactive-companion-export.json");
      toast("已导出");
    } catch (err) {
      toast(err.message || "导出失败", true);
    }
  });

  $("#session-search").addEventListener("input", () => {
    clearTimeout(loadSessions._t);
    loadSessions._t = setTimeout(() => {
      loadSessions().catch((e) => toast(e.message, true));
    }, 260);
  });

  $("#session-rows").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const tr = btn.closest("tr");
    const key = tr?.dataset.key;
    if (key) sessionAction(key, btn.dataset.act, btn);
  });

  $("#profile-rows").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-act='edit-profile']");
    if (!btn) return;
    const tr = btn.closest("tr");
    const profile = state.profiles.find(
      (p) =>
        p.session_key === tr?.dataset.key && p.user_id === tr?.dataset.user
    );
    if (profile) openProfileEditor(profile);
  });

  $("#btn-save-profile").addEventListener("click", saveProfile);

  const rebuildBtn = $("#btn-rebuild-profiles");
  if (rebuildBtn) {
    rebuildBtn.addEventListener("click", async () => {
      rebuildBtn.disabled = true;
      try {
        const data = await bridge.apiPost("profiles/rebuild", {});
        toast(`已用历史记录补全 ${data.rebuilt ?? 0} 份画像`);
        await loadProfiles();
      } catch (err) {
        toast(err.message || "补全失败", true);
      } finally {
        rebuildBtn.disabled = false;
      }
    });
  }
}

async function main() {
  if (!bridge) {
    document.body.innerHTML = '<p style="padding:24px">未检测到 AstrBot 视图桥接 SDK，请在 AstrBot WebUI 中打开本插件视图。</p>';
    return;
  }
  try {
    const ctx = await bridge.ready();
    const title = bridge.t ? bridge.t("views.panel.title", "主动陪伴") : "主动陪伴";
    document.title = title;
    const heading = $("#title");
    if (heading) heading.textContent = title;
  } catch (err) {
    console.warn("bridge context unavailable", err);
  }

  bindTabs();
  bindActions();

  try {
    await Promise.all([loadState(), loadSessions(), loadEvents(), loadProfiles()]);
  } catch (err) {
    toast(err.message || "加载失败", true);
  }

  // 定期刷新概览
  setInterval(() => {
    loadState().catch(() => {});
  }, 30000);
}

main();
