import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Copy, RefreshCw, RotateCcw, Search, Trash2, UserPlus, X } from "lucide-react";
import { apiFetch } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { TaskLogPanel } from "@/components/tasks/TaskLogPanel";

type MailboxAccount = {
  id: string;
  provider: string;
  email: string;
  login_account?: string;
  status?: string;
  credentials?: Record<string, any>;
  capabilities?: Record<string, any>;
  usage?: Record<string, any>;
  metadata?: Record<string, any>;
  updated_at?: string;
};

type MailboxAddress = {
  id: string;
  mailbox_account_id: string;
  address: string;
  address_type?: string;
  status?: string;
  reserved?: boolean;
  reserved_for?: Record<string, any>;
  metadata?: Record<string, any>;
  updated_at?: string;
};

type AccountMailboxLink = {
  id: string;
  platform: string;
  account_id: number;
  account_email?: string;
  mailbox_address_id: string;
  mailbox_account_id: string;
  purpose?: string;
  status?: string;
  created_at?: string;
};

type MailboxResource = {
  id: string;
  resource_kind: "address";
  mailbox_account_id: string;
  mailbox_address_id: string;
  address: string;
  address_type?: string;
  provider: string;
  parent_email?: string;
  login_account?: string;
  status: string;
  mailbox_status?: string;
  reserved?: boolean;
  reserved_for?: Record<string, any>;
  usage?: Record<string, any>;
  chatgpt_account_id?: number | null;
  chatgpt_account_email?: string;
  link_id?: string;
  allocation_id?: string;
  allocation_status?: string;
  allocation_reason?: string;
  updated_at?: string;
  created_at?: string;
};

type MailMessage = {
  id?: string;
  subject?: string;
  from?: string;
  to?: string[];
  received_at?: string;
  preview?: string;
  code?: string;
  link?: string;
  folder?: string;
  provider?: string;
};

type MailboxPayload = {
  resources: MailboxResource[];
  accounts: MailboxAccount[];
  addresses: MailboxAddress[];
  links: AccountMailboxLink[];
  paths?: Record<string, string>;
};

type MailboxRegistrationOptions = {
  executorType: "browser_protocol" | "browser";
  visibleWindow: boolean;
  proxyMode: "direct" | "proxy_service" | "manual";
  proxyValue: string;
};

const MAILBOX_REGISTER_OPTIONS_KEY = "fuckgpt.mailbox-register-options";

const statusOptions = [
  { value: "all", label: "全部状态" },
  { value: "available", label: "空闲" },
  { value: "allocated", label: "已分配" },
  { value: "bound", label: "已绑定" },
  { value: "expired", label: "已过期" },
  { value: "archived", label: "已归档" },
  { value: "disabled", label: "禁用" },
];

function statusLabel(status?: string) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "bound" || normalized === "registered") return "已绑定";
  if (normalized === "allocated") return "已分配";
  if (normalized === "available") return "空闲";
  if (normalized === "expired") return "已过期";
  if (normalized === "archived") return "已归档";
  if (normalized === "active") return "进行中";
  if (normalized === "succeeded") return "成功";
  if (normalized === "failed") return "失败（已回池）";
  if (normalized === "cancelled") return "已取消（已回池）";
  if (normalized === "interrupted") return "已中断（已回池）";
  if (normalized === "disabled" || normalized === "inactive") return "禁用";
  if (normalized === "error") return "异常";
  return status || "未知";
}

function statusPillClass(status?: string) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "bound" || normalized === "registered") return "bg-emerald-500/10 text-emerald-500 ring-emerald-500/20";
  if (normalized === "allocated") return "bg-amber-500/10 text-amber-500 ring-amber-500/20";
  if (["disabled", "inactive", "error", "expired"].includes(normalized)) return "bg-red-500/10 text-red-500 ring-red-500/20";
  if (normalized === "archived") return "bg-slate-500/10 text-slate-400 ring-slate-500/20";
  if (normalized === "available") return "bg-blue-500/10 text-blue-500 ring-blue-500/20";
  return "bg-[var(--text-primary)]/5 text-[var(--text-secondary)] ring-[var(--border)]";
}

function statusDotClass(status?: string) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "bound" || normalized === "registered") return "bg-emerald-500 shadow-[0_0_4px_rgba(16,185,129,0.6)]";
  if (normalized === "allocated") return "bg-amber-500 shadow-[0_0_4px_rgba(245,158,11,0.6)]";
  if (["disabled", "inactive", "error", "expired"].includes(normalized)) return "bg-red-500 shadow-[0_0_4px_rgba(239,68,68,0.6)]";
  if (normalized === "archived") return "bg-slate-400";
  if (normalized === "available") return "bg-blue-500";
  return "bg-gray-400";
}

function providerLabel(provider?: string) {
  if (provider === "local_ms_pool") return "local_ms";
  return provider || "-";
}

function formatTime(value?: string) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function formatCompactTime(value?: string) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function jsonBlock(value: unknown) {
  return JSON.stringify(value ?? {}, null, 2);
}

function StatusPill({ status }: { status?: string }) {
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${statusPillClass(status)}`}>
      <span className={`mr-1 h-1 w-1 rounded-full ${statusDotClass(status)}`} />
      {statusLabel(status)}
    </span>
  );
}

function ActionMenu({
  resource,
  onDetail,
  onAction,
}: {
  resource: MailboxResource;
  onDetail: () => void;
  onAction: (resource: MailboxResource, action: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0, maxHeight: 320 });
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const update = () => {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const width = 180;
      const gap = 6;
      const desiredHeight = 260;
      const spaceBelow = window.innerHeight - rect.bottom - 16;
      const spaceAbove = rect.top - 16;
      const openUp = spaceBelow < desiredHeight && spaceAbove > spaceBelow;
      const maxHeight = Math.max(160, Math.min(desiredHeight, openUp ? spaceAbove : spaceBelow));
      setMenuPosition({
        top: openUp ? Math.max(8, rect.top - maxHeight - gap) : rect.bottom + gap,
        left: Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8)),
        maxHeight,
      });
    };
    const close = (event: MouseEvent) => {
      const target = event.target as Node;
      if (triggerRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    document.addEventListener("mousedown", close);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
      document.removeEventListener("mousedown", close);
    };
  }, [open]);

  const run = (action: string) => {
    setOpen(false);
    onAction(resource, action);
  };
  const canRegister = resource.status === "available" && !resource.chatgpt_account_id;

  return (
    <div className="flex items-center justify-end gap-2">
      <button onClick={onDetail} className="table-action-btn">详情</button>
      <button ref={triggerRef} onClick={() => setOpen((value) => !value)} className="table-action-btn">更多 ▾</button>
      {open && typeof document !== "undefined" && createPortal(
        <div
          ref={menuRef}
          className="fixed z-[9999] w-[180px] overflow-y-auto rounded-2xl border border-[var(--border)] bg-[var(--bg-card)]/96 py-1.5 shadow-[var(--shadow-soft)]"
          style={{ top: menuPosition.top, left: menuPosition.left, maxHeight: menuPosition.maxHeight }}
        >
          <button onClick={() => run("copy")} className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]">
            复制邮箱
          </button>
          <button onClick={() => run("messages")} className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]">
            获取邮件
          </button>
          <button
            onClick={() => run("register")}
            disabled={!canRegister}
            className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] disabled:opacity-40"
          >
            用此邮箱注册
          </button>
          <button
            onClick={() => run("release")}
            disabled={resource.status !== "allocated"}
            className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] disabled:opacity-40"
          >
            释放邮箱
          </button>
          <div className="my-1 border-t border-[var(--border)]/70" />
          <button onClick={() => run("delete")} className="w-full px-3 py-2 text-left text-xs text-[#f0b0b0] transition-colors hover:bg-[rgba(239,68,68,0.08)] hover:text-[#ffd5d5]">
            归档记录
          </button>
        </div>,
        document.body,
      )}
    </div>
  );
}

function MailboxRegisterConfigModal({
  resources,
  onStart,
  onClose,
}: {
  resources: MailboxResource[];
  onStart: (options: MailboxRegistrationOptions) => Promise<void>;
  onClose: () => void;
}) {
  const [options, setOptions] = useState<MailboxRegistrationOptions>(() => {
    if (typeof window !== "undefined") {
      try {
        const saved = JSON.parse(window.localStorage.getItem(MAILBOX_REGISTER_OPTIONS_KEY) || "{}");
        return {
          executorType: ["browser_protocol", "browser"].includes(saved.executorType)
            ? saved.executorType
            : "browser",
          visibleWindow: Boolean(saved.visibleWindow) || saved.executorType === "headed",
          proxyMode: ["direct", "proxy_service", "manual"].includes(saved.proxyMode) ? saved.proxyMode : "direct",
          proxyValue: String(saved.proxyValue || ""),
        };
      } catch {
        // Ignore invalid legacy browser storage and fall back to safe defaults.
      }
    }
    return { executorType: "browser", visibleWindow: false, proxyMode: "direct", proxyValue: "" };
  });
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");

  const start = async () => {
    const proxyValue = options.proxyValue.trim();
    if (options.proxyMode === "manual" && !proxyValue) {
      setError("手动代理模式需要填写代理 URL");
      return;
    }
    setStarting(true);
    setError("");
    const normalized = { ...options, proxyValue };
    try {
      window.localStorage.setItem(MAILBOX_REGISTER_OPTIONS_KEY, JSON.stringify(normalized));
      await onStart(normalized);
    } catch (exc: any) {
      setError(exc?.message || "创建注册任务失败");
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="dialog-backdrop" onClick={starting ? undefined : onClose}>
      <div className="dialog-panel dialog-panel-md" onClick={(event) => event.stopPropagation()}>
        <div className="flex items-start justify-between gap-4 border-b border-[var(--border)] px-6 py-5">
          <div className="min-w-0">
            <h2 className="text-lg font-semibold text-[var(--text-primary)]">配置固定邮箱注册</h2>
            <p className="mt-1 text-xs text-[var(--text-muted)]">
              将为 {resources.length} 个空闲邮箱分别创建注册任务
            </p>
          </div>
          <button disabled={starting} onClick={onClose} className="rounded-full border border-[var(--border)] bg-[var(--bg-hover)] p-2 text-[var(--text-muted)] hover:text-[var(--text-primary)] disabled:opacity-40">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="space-y-4 px-6 py-5">
          <div>
            <label className="mb-1 block text-xs text-[var(--text-muted)]">执行方式</label>
            <select
              value={options.executorType}
              onChange={(event) => setOptions((current) => ({ ...current, executorType: event.target.value as MailboxRegistrationOptions["executorType"] }))}
              className="control-surface control-surface-compact appearance-none"
            >
              <option value="browser_protocol">浏览器协议模式</option>
              <option value="browser">浏览器模式</option>
            </select>
          </div>
          {(["browser_protocol", "browser"] as const).includes(options.executorType) ? (
            <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-[var(--border)] bg-[var(--bg-pane)]/45 px-4 py-3">
              <input
                type="checkbox"
                checked={options.visibleWindow}
                onChange={(event) => setOptions((current) => ({ ...current, visibleWindow: event.target.checked }))}
                className="mt-0.5 h-4 w-4 accent-[var(--accent)]"
              />
              <span>
                <span className="block text-sm font-medium text-[var(--text-primary)]">显示浏览器窗口</span>
                <span className="mt-1 block text-xs text-[var(--text-muted)]">关闭时后台运行，开启后显示 Camoufox 自动化窗口。</span>
              </span>
            </label>
          ) : null}
          <div>
            <label className="mb-1 block text-xs text-[var(--text-muted)]">ChatGPT/Codex 代理</label>
            <select
              value={options.proxyMode}
              onChange={(event) => setOptions((current) => ({ ...current, proxyMode: event.target.value as MailboxRegistrationOptions["proxyMode"] }))}
              className="control-surface control-surface-compact appearance-none"
            >
              <option value="direct">直连</option>
              <option value="proxy_service">使用代理服务</option>
              <option value="manual">手动填写</option>
            </select>
            {options.proxyMode === "manual" ? (
              <input
                value={options.proxyValue}
                onChange={(event) => setOptions((current) => ({ ...current, proxyValue: event.target.value }))}
                placeholder="http://127.0.0.1:7897"
                spellCheck={false}
                className="control-surface control-surface-compact mt-2 w-full font-mono text-xs"
              />
            ) : null}
          </div>
          <div className="max-h-28 overflow-y-auto rounded-lg border border-[var(--border)] bg-[var(--bg-hover)]/40 px-3 py-2 text-xs text-[var(--text-secondary)]">
            {resources.map((resource) => <div key={resource.id} className="truncate">{resource.address}</div>)}
          </div>
          {error ? <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-400">{error}</div> : null}
        </div>
        <div className="flex justify-end gap-2 border-t border-[var(--border)] px-6 py-4">
          <Button variant="outline" onClick={onClose} disabled={starting}>取消</Button>
          <Button onClick={start} disabled={starting}>{starting ? "创建中" : "开始注册"}</Button>
        </div>
      </div>
    </div>
  );
}

function MailboxRegisterTaskModal({
  resource,
  taskId,
  taskStatus,
  onDone,
  onClose,
}: {
  resource: MailboxResource;
  taskId: string;
  taskStatus: string | null;
  onDone: (status: string) => void;
  onClose: () => void;
}) {
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel flex w-[min(960px,calc(100vw-32px))] max-w-none flex-col overflow-hidden"
        style={{ maxHeight: "90vh" }}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 border-b border-[var(--border)] px-6 py-5">
          <div className="min-w-0">
            <div className="mb-2 inline-flex items-center gap-1.5 rounded-full border border-[var(--border)] bg-[var(--chip-bg)] px-3 py-1 text-[11px] uppercase tracking-[0.16em] text-[var(--text-muted)]">
              <UserPlus className="h-3.5 w-3.5" />
              Register
            </div>
            <h2 className="truncate text-lg font-semibold text-[var(--text-primary)]">用选中邮箱注册</h2>
            <p className="mt-1 break-all text-xs text-[var(--text-muted)]">{resource.address}</p>
          </div>
          <div className="flex items-center gap-2">
            {taskStatus ? (
              <span className="rounded-full border border-[var(--border)] bg-[var(--bg-hover)] px-2.5 py-1 text-xs text-[var(--text-secondary)]">
                {taskStatus}
              </span>
            ) : null}
            <button onClick={onClose} className="rounded-full border border-[var(--border)] bg-[var(--bg-hover)] p-2 text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto px-6 py-5">
          <TaskLogPanel taskId={taskId} onDone={onDone} />
        </div>
        <div className="flex items-center justify-between border-t border-[var(--border)] px-6 py-3 text-xs text-[var(--text-muted)]">
          <span>Task: {taskId}</span>
          <Button variant="outline" size="sm" onClick={onClose}>关闭</Button>
        </div>
      </div>
    </div>
  );
}

function MailMessagesModal({
  resource,
  messages,
  loading,
  error,
  onRefresh,
  onClose,
}: {
  resource: MailboxResource;
  messages: MailMessage[];
  loading: boolean;
  error: string;
  onRefresh: () => void;
  onClose: () => void;
}) {
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-lg flex flex-col" style={{ maxHeight: "90vh" }} onClick={(event) => event.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-[var(--border)] px-6 py-4 shrink-0">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">邮件列表</h2>
            <p className="mt-0.5 break-all text-xs text-[var(--text-muted)]">{resource.address}</p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={onRefresh} disabled={loading}>
              {loading ? "获取中" : "重新获取"}
            </Button>
            <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
          {error ? (
            <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-400">
              {error}
            </div>
          ) : null}
          {!error && loading && messages.length === 0 ? (
            <div className="empty-state-panel">正在获取邮件...</div>
          ) : null}
          {!error && !loading && messages.length === 0 ? (
            <div className="empty-state-panel">没有获取到邮件。</div>
          ) : null}
          {messages.length > 0 ? (
            <div className="space-y-3">
              {messages.map((message, index) => (
                <div key={message.id || index} className="rounded-lg border border-[var(--border)] bg-[var(--bg-hover)] p-3">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium text-[var(--text-primary)]" title={message.subject || ""}>
                        {message.subject || "(无主题)"}
                      </div>
                      <div className="mt-1 flex flex-wrap gap-2 text-xs text-[var(--text-muted)]">
                        <span>{message.from || "未知发件人"}</span>
                        <span>{formatTime(message.received_at)}</span>
                        {message.folder ? <span>{message.folder}</span> : null}
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {message.code ? <span className="rounded border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-500">code {message.code}</span> : null}
                      {message.link ? <span className="rounded border border-blue-500/20 bg-blue-500/10 px-2 py-0.5 text-xs text-blue-500">link</span> : null}
                    </div>
                  </div>
                  {message.to?.length ? (
                    <div className="mt-2 truncate text-xs text-[var(--text-muted)]" title={message.to.join(", ")}>
                      To: {message.to.join(", ")}
                    </div>
                  ) : null}
                  <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-[var(--border)] bg-[var(--bg-input)] p-2 text-xs text-[var(--text-secondary)]">
                    {message.preview || ""}
                  </pre>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function DetailModal({
  resource,
  account,
  address,
  link,
  onClose,
}: {
  resource: MailboxResource;
  account: MailboxAccount | null;
  address: MailboxAddress | null;
  link: AccountMailboxLink | null;
  onClose: () => void;
}) {
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-lg flex flex-col" style={{ maxHeight: "90vh" }} onClick={(event) => event.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-[var(--border)] px-6 py-4 shrink-0">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">邮箱详情</h2>
            <p className="mt-0.5 break-all text-xs text-[var(--text-muted)]">{resource.address}</p>
          </div>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
          <div className="grid gap-4 lg:grid-cols-[0.75fr_1.25fr]">
            <div className="space-y-4">
              <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-hover)] p-4">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <StatusPill status={resource.status} />
                  <span className="text-xs text-[var(--text-muted)]">{providerLabel(resource.provider)}</span>
                </div>
                <div className="grid gap-3 text-sm">
                  <div>
                    <div className="text-xs text-[var(--text-muted)]">邮箱号</div>
                    <div className="mt-1 break-all font-mono text-[var(--text-primary)]">{resource.address}</div>
                  </div>
                  <div>
                    <div className="text-xs text-[var(--text-muted)]">来源账号</div>
                    <div className="mt-1 break-all text-[var(--text-primary)]">{resource.parent_email || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs text-[var(--text-muted)]">登录账号</div>
                    <div className="mt-1 break-all text-[var(--text-primary)]">{resource.login_account || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs text-[var(--text-muted)]">ChatGPT 账号</div>
                    <div className="mt-1 text-[var(--text-primary)]">
                      {resource.chatgpt_account_id ? `#${resource.chatgpt_account_id}` : "未绑定"}
                    </div>
                    {resource.chatgpt_account_email ? (
                      <div className="mt-1 break-all text-xs text-[var(--text-muted)]">{resource.chatgpt_account_email}</div>
                    ) : null}
                  </div>
                  <div>
                    <div className="text-xs text-[var(--text-muted)]">最近分配历史</div>
                    <div className="mt-1 text-[var(--text-primary)]">
                      {resource.allocation_status ? statusLabel(resource.allocation_status) : "暂无"}
                    </div>
                    {resource.allocation_reason ? (
                      <div className="mt-1 break-all text-xs text-[var(--text-muted)]">{resource.allocation_reason}</div>
                    ) : null}
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div>
                      <div className="text-xs text-[var(--text-muted)]">地址类型</div>
                      <div className="mt-1 text-[var(--text-primary)]">{resource.address_type || "primary"}</div>
                    </div>
                    <div>
                      <div className="text-xs text-[var(--text-muted)]">更新时间</div>
                      <div className="mt-1 text-[var(--text-primary)]">{formatTime(resource.updated_at || resource.created_at)}</div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <details open className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)]">
              <summary className="cursor-pointer px-3 py-2 text-sm font-medium text-[var(--text-primary)]">原始数据</summary>
              <div className="grid gap-3 border-t border-[var(--border)] p-3 md:grid-cols-3">
                <div>
                  <div className="mb-1 text-xs text-[var(--text-muted)]">account</div>
                  <pre className="control-surface control-surface-mono max-h-80 overflow-auto whitespace-pre-wrap">{jsonBlock(account)}</pre>
                </div>
                <div>
                  <div className="mb-1 text-xs text-[var(--text-muted)]">address</div>
                  <pre className="control-surface control-surface-mono max-h-80 overflow-auto whitespace-pre-wrap">{jsonBlock(address)}</pre>
                </div>
                <div>
                  <div className="mb-1 text-xs text-[var(--text-muted)]">link</div>
                  <pre className="control-surface control-surface-mono max-h-80 overflow-auto whitespace-pre-wrap">{jsonBlock(link)}</pre>
                </div>
              </div>
            </details>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function MailboxResources() {
  const [payload, setPayload] = useState<MailboxPayload>({
    resources: [],
    accounts: [],
    addresses: [],
    links: [],
  });
  const [statusFilter, setStatusFilter] = useState("all");
  const [providerFilter, setProviderFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [detail, setDetail] = useState<MailboxResource | null>(null);
  const [mailDialog, setMailDialog] = useState<{ resource: MailboxResource; messages: MailMessage[]; loading: boolean; error: string } | null>(null);
  const [registerCandidates, setRegisterCandidates] = useState<MailboxResource[]>([]);
  const [registerTask, setRegisterTask] = useState<{ resource: MailboxResource; taskId: string; status: string | null } | null>(null);
  const [loading, setLoading] = useState(false);
  const [bulkAction, setBulkAction] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const accountById = useMemo(() => new Map(payload.accounts.map((item) => [item.id, item])), [payload.accounts]);
  const addressById = useMemo(() => new Map(payload.addresses.map((item) => [item.id, item])), [payload.addresses]);
  const linkByAddressId = useMemo(() => new Map(payload.links.map((item) => [item.mailbox_address_id, item])), [payload.links]);

  const providers = useMemo(() => {
    return Array.from(new Set(payload.resources.map((item) => item.provider).filter(Boolean))).sort();
  }, [payload.resources]);

  const counts = useMemo(() => {
    return payload.resources.reduce<Record<string, number>>((acc, item) => {
      acc[item.status] = (acc[item.status] || 0) + 1;
      return acc;
    }, {});
  }, [payload.resources]);

  const visibleResources = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return payload.resources.filter((item) => {
      if (statusFilter === "all" && item.status === "archived") return false;
      if (statusFilter !== "all" && item.status !== statusFilter) return false;
      if (providerFilter !== "all" && item.provider !== providerFilter) return false;
      if (needle) {
        const haystack = [
          item.address,
          item.parent_email,
          item.login_account,
          item.provider,
          item.chatgpt_account_email,
          item.chatgpt_account_id ? String(item.chatgpt_account_id) : "",
        ].join(" ").toLowerCase();
        if (!haystack.includes(needle)) return false;
      }
      return true;
    });
  }, [payload.resources, providerFilter, search, statusFilter]);

  const selectedResources = useMemo(() => {
    return payload.resources.filter((item) => selectedIds.has(item.id));
  }, [payload.resources, selectedIds]);
  const visibleIds = useMemo(() => visibleResources.map((item) => item.id), [visibleResources]);
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedIds.has(id));
  const selectedCount = selectedIds.size;
  const selectedRegistrableCount = selectedResources.filter((item) => item.status === "available" && !item.chatgpt_account_id).length;

  useEffect(() => {
    setSelectedIds((current) => {
      const existing = new Set(payload.resources.map((item) => item.id));
      const next = new Set([...current].filter((id) => existing.has(id)));
      return next.size === current.size ? current : next;
    });
  }, [payload.resources]);

  const toggleOne = (id: string) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleVisible = () => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (allVisibleSelected) visibleIds.forEach((id) => next.delete(id));
      else visibleIds.forEach((id) => next.add(id));
      return next;
    });
  };

  const load = async () => {
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const data = await apiFetch("/mailboxes");
      setPayload({
        resources: Array.isArray(data?.resources) ? data.resources : [],
        accounts: Array.isArray(data?.accounts) ? data.accounts : [],
        addresses: Array.isArray(data?.addresses) ? data.addresses : [],
        links: Array.isArray(data?.links) ? data.links : [],
        paths: data?.paths || {},
      });
    } catch (exc: any) {
      setError(exc?.message || "加载邮箱资源失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const copyAddress = async (address: string) => {
    if (!address || !navigator?.clipboard) return;
    await navigator.clipboard.writeText(address);
  };

  const releaseAddress = async (resource: MailboxResource) => {
    if (!resource.mailbox_address_id) return;
    if (!confirm(`释放邮箱 ${resource.address}？`)) return;
    await apiFetch(`/mailboxes/addresses/${resource.mailbox_address_id}/release`, { method: "POST" });
    await load();
  };

  const deleteMailbox = async (resource: MailboxResource) => {
    if (!confirm(`归档邮箱 ${resource.address}？资源会从日常使用中隐藏，但分配和绑定历史会保留。`)) return;
    await apiFetch(`/mailboxes/accounts/${resource.mailbox_account_id}`, { method: "DELETE" });
    if (detail?.id === resource.id) setDetail(null);
    await load();
  };

  const fetchMessages = async (resource: MailboxResource) => {
    setMailDialog({ resource, messages: [], loading: true, error: "" });
    try {
      const data = await apiFetch(`/mailboxes/addresses/${resource.mailbox_address_id}/messages?limit=10`);
      setMailDialog({ resource, messages: Array.isArray(data?.items) ? data.items : [], loading: false, error: "" });
    } catch (exc: any) {
      setMailDialog({ resource, messages: [], loading: false, error: exc?.message || "获取邮件失败" });
    }
  };

  const registerWithMailbox = async (resource: MailboxResource) => {
    if (!resource.mailbox_address_id) return;
    if (resource.status === "bound" || resource.status === "registered" || resource.chatgpt_account_id) {
      throw new Error("该邮箱已注册或已绑定账号");
    }
    setRegisterCandidates([resource]);
  };

  const createRegisterTask = async (resource: MailboxResource, options: MailboxRegistrationOptions) => {
    return apiFetch("/tasks/register", {
      method: "POST",
      body: JSON.stringify({
        email: resource.address,
        count: 1,
        concurrency: 1,
        executor_type: options.executorType,
        captcha_solver: "auto",
        proxy: options.proxyMode === "manual" ? options.proxyValue : null,
        platform_proxy_mode: options.proxyMode,
        platform_proxy_value: options.proxyValue,
        mailbox_proxy_mode: "direct",
        mailbox_proxy_value: "",
        extra: {
          identity_provider: "mailbox",
          mail_provider: resource.provider,
          mailbox_address_id: resource.mailbox_address_id,
          prefer_password_registration: true,
          browser_visible: options.visibleWindow,
        },
      }),
    });
  };

  const startRegisterTasks = async (options: MailboxRegistrationOptions) => {
    const candidates = [...registerCandidates];
    if (candidates.length === 0) return;
    const results = await Promise.allSettled(candidates.map((item) => createRegisterTask(item, options)));
    const taskIds = results
      .filter((item): item is PromiseFulfilledResult<any> => item.status === "fulfilled")
      .map((item) => String(item.value?.task_id || ""))
      .filter(Boolean);
    const firstTask = taskIds[0];
    if (!firstTask) {
      const firstError = results.find((item): item is PromiseRejectedResult => item.status === "rejected");
      throw firstError?.reason || new Error("注册任务创建失败");
    }
    setRegisterCandidates([]);
    setRegisterTask({ resource: candidates[0], taskId: firstTask, status: null });
    setNotice(`已创建 ${taskIds.length}/${candidates.length} 个注册任务`);
    setSelectedIds(new Set());
  };

  const runBulkAction = async (action: string) => {
    const items = selectedResources;
    if (items.length === 0) return;
    setBulkAction(action);
    setError("");
    setNotice("");
    try {
      if (action === "copy") {
        await navigator.clipboard.writeText(items.map((item) => item.address).join("\n"));
        setNotice(`已复制 ${items.length} 个邮箱`);
        return;
      }
      if (action === "register") {
        const candidates = items.filter((item) => item.status === "available" && !item.chatgpt_account_id);
        if (candidates.length === 0) throw new Error("选中项里没有可注册邮箱");
        setRegisterCandidates(candidates);
        return;
      }
      if (action === "release") {
        const candidates = items.filter((item) => item.status === "allocated");
        if (candidates.length === 0) throw new Error("选中项里没有可释放邮箱");
        if (!confirm(`释放 ${candidates.length} 个选中邮箱？`)) return;
        await Promise.all(candidates.map((item) => apiFetch(`/mailboxes/addresses/${item.mailbox_address_id}/release`, { method: "POST" })));
        setNotice(`已释放 ${candidates.length} 个邮箱`);
      }
      if (action === "delete") {
        const accountIds = Array.from(new Set(items.map((item) => item.mailbox_account_id).filter(Boolean)));
        if (accountIds.length === 0) throw new Error("选中项里没有可归档资源");
        if (!confirm(`归档 ${accountIds.length} 个邮箱资源？分配和绑定历史会保留。`)) return;
        await Promise.all(accountIds.map((id) => apiFetch(`/mailboxes/accounts/${id}`, { method: "DELETE" })));
        setNotice(`已归档 ${accountIds.length} 个邮箱资源`);
      }
      setSelectedIds(new Set());
      await load();
    } catch (exc: any) {
      setError(exc?.message || "批量操作失败");
    } finally {
      setBulkAction("");
    }
  };

  const handleAction = async (resource: MailboxResource, action: string) => {
    setError("");
    setNotice("");
    try {
      if (action === "copy") await copyAddress(resource.address);
      if (action === "messages") await fetchMessages(resource);
      if (action === "register") await registerWithMailbox(resource);
      if (action === "release") await releaseAddress(resource);
      if (action === "delete") await deleteMailbox(resource);
    } catch (exc: any) {
      setError(exc?.message || "操作失败");
    }
  };

  const detailAccount = detail ? accountById.get(detail.mailbox_account_id) || null : null;
  const detailAddress = detail ? addressById.get(detail.mailbox_address_id) || null : null;
  const detailLink = detail ? linkByAddressId.get(detail.mailbox_address_id) || null : null;

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold text-[var(--text-primary)]">邮箱资源</h1>
          <div className="mt-1 text-[12px] text-[var(--text-muted)]">
            {payload.paths?.accounts ? <span className="break-all">JSON: {payload.paths.accounts}</span> : "本地邮箱资源"}
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          <RefreshCw className="mr-2 h-4 w-4" />
          {loading ? "刷新中" : "刷新列表"}
        </Button>
      </div>

      {error ? (
        <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      ) : null}
      {notice ? (
        <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-400">
          {notice}
        </div>
      ) : null}

      <Card className="shrink-0 bg-[var(--bg-pane)]/40 border border-[var(--border)] shadow-sm">
        <div className="flex flex-wrap items-center gap-2 p-3">
          <div className="relative w-full min-w-[220px] sm:w-72">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--text-muted)]" />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索邮箱、来源、账号"
              className="control-surface control-surface-compact pl-8"
            />
          </div>
          <select className="control-surface control-surface-compact w-auto min-w-28" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            {statusOptions.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
          <select className="control-surface control-surface-compact w-auto min-w-32" value={providerFilter} onChange={(event) => setProviderFilter(event.target.value)}>
            <option value="all">全部 Provider</option>
            {providers.map((provider) => (
              <option key={provider} value={provider}>{provider}</option>
            ))}
          </select>
          <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--text-muted)]">
            <span className="rounded border border-[var(--border)] bg-[var(--bg-card)] px-2 py-1">共 {payload.resources.length}</span>
            {selectedCount ? <span className="rounded border border-[var(--accent-edge)] bg-[var(--accent-soft)] px-2 py-1 text-[var(--text-primary)]">已选 {selectedCount}</span> : null}
            <span className="rounded border border-[var(--border)] bg-[var(--bg-card)] px-2 py-1">已绑定 {counts.bound || counts.registered || 0}</span>
            <span className="rounded border border-[var(--border)] bg-[var(--bg-card)] px-2 py-1">已分配 {counts.allocated || 0}</span>
            <span className="rounded border border-[var(--border)] bg-[var(--bg-card)] px-2 py-1">空闲 {counts.available || 0}</span>
          </div>
          <div className="ml-auto text-xs text-[var(--text-muted)]">当前 {visibleResources.length}</div>
        </div>
        {selectedCount > 0 ? (
          <div className="flex flex-wrap items-center gap-2 border-t border-[var(--border)] px-3 py-2">
            <span className="mr-1 text-xs text-[var(--text-muted)]">选中 {selectedCount}</span>
            <Button variant="outline" size="sm" onClick={() => runBulkAction("copy")} disabled={Boolean(bulkAction)} className="h-7 px-2.5 text-xs">
              <Copy className="mr-1.5 h-3.5 w-3.5" />
              复制
            </Button>
            <Button variant="outline" size="sm" onClick={() => runBulkAction("register")} disabled={Boolean(bulkAction) || selectedRegistrableCount === 0} className="h-7 px-2.5 text-xs">
              <UserPlus className="mr-1.5 h-3.5 w-3.5" />
              注册可用({selectedRegistrableCount})
            </Button>
            <Button variant="outline" size="sm" onClick={() => runBulkAction("release")} disabled={Boolean(bulkAction)} className="h-7 px-2.5 text-xs">
              <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
              释放
            </Button>
            <Button variant="outline" size="sm" onClick={() => runBulkAction("delete")} disabled={Boolean(bulkAction)} className="h-7 px-2.5 text-xs text-red-400 hover:text-red-300">
              <Trash2 className="mr-1.5 h-3.5 w-3.5" />
              归档资源
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setSelectedIds(new Set())} disabled={Boolean(bulkAction)} className="h-7 px-2.5 text-xs">
              取消选择
            </Button>
          </div>
        ) : null}
      </Card>

      <Card className="min-h-0 flex-1 overflow-hidden p-0 border border-[var(--border)] shadow-sm">
        <div className="glass-table-wrap h-full min-h-0 overflow-auto">
          <table className="table-fixed w-full min-w-[960px] text-sm">
            <colgroup>
              <col className="w-10" />
              <col className="w-[36%]" />
              <col className="w-[11%]" />
              <col className="w-[11%]" />
              <col className="w-[18%]" />
              <col className="w-[10%]" />
              <col className="w-[12%]" />
            </colgroup>
            <thead className="sticky top-0 z-10 bg-[var(--bg-pane)]/80">
              <tr className="border-b border-[var(--border)] text-xs font-medium uppercase tracking-wider text-[var(--text-muted)]">
                <th className="w-10 px-3 py-2 text-left">
                  <input
                    type="checkbox"
                    checked={allVisibleSelected}
                    onChange={toggleVisible}
                    className="checkbox-accent rounded-[3px] border-[var(--border)] bg-transparent text-[var(--text-primary)] focus:ring-[var(--text-primary)] focus:ring-offset-0"
                  />
                </th>
                <th className="px-3 py-2 text-left">邮箱号</th>
                <th className="px-3 py-2 text-left">状态</th>
                <th className="px-3 py-2 text-left">Provider</th>
                <th className="px-3 py-2 text-left">ChatGPT 账号</th>
                <th className="px-3 py-2 text-left">更新时间</th>
                <th className="px-3 py-2 text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {visibleResources.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-4 py-24 text-center">
                    <div className="flex flex-col items-center justify-center space-y-3">
                      <div className="flex h-12 w-12 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--bg-pane)] shadow-sm">
                        <span className="text-lg text-[var(--text-muted)]">@</span>
                      </div>
                      <h3 className="text-sm font-medium text-[var(--text-primary)]">暂无邮箱资源</h3>
                      <p className="max-w-sm text-xs text-[var(--text-muted)]">获取到邮箱后会出现在这里。</p>
                    </div>
                  </td>
                </tr>
              )}
              {visibleResources.map((resource) => (
                <tr
                  key={resource.id}
                  className={`group cursor-pointer border-b border-[var(--border)]/30 transition-colors hover:bg-[var(--text-primary)]/[0.02] ${selectedIds.has(resource.id) ? "bg-[var(--accent-soft)]" : ""}`}
                  onClick={() => setDetail(resource)}
                >
                  <td className="px-3 py-2.5 align-top" onClick={(event) => event.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={selectedIds.has(resource.id)}
                      onChange={() => toggleOne(resource.id)}
                      className="checkbox-accent rounded-[3px] border-[var(--border)] bg-transparent text-[var(--text-primary)] focus:ring-[var(--text-primary)] focus:ring-offset-0"
                    />
                  </td>
                  <td className="px-3 py-2.5 align-top font-mono text-sm text-[var(--text-primary)]">
                    <div className="min-w-0">
                      <div className="truncate tracking-tight" title={resource.address}>{resource.address}</div>
                      <div className="mt-1 truncate text-xs text-[var(--text-muted)]">
                        {resource.address_type || "primary"}
                      </div>
                    </div>
                  </td>
                  <td className="px-3 py-2.5 align-top">
                    <StatusPill status={resource.status} />
                  </td>
                  <td className="px-3 py-2.5 align-top text-[var(--text-secondary)]">
                    <div className="truncate" title={resource.provider}>{providerLabel(resource.provider)}</div>
                  </td>
                  <td className="px-3 py-2.5 align-top">
                    {resource.chatgpt_account_id ? (
                      <div className="min-w-0">
                        <div className="font-medium text-[var(--text-primary)]">#{resource.chatgpt_account_id}</div>
                        <div className="mt-1 truncate text-xs text-[var(--text-muted)]" title={resource.chatgpt_account_email || ""}>
                          {resource.chatgpt_account_email || "未记录邮箱"}
                        </div>
                      </div>
                    ) : (
                      <span className="text-xs text-[var(--text-muted)]">未绑定</span>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2.5 align-top font-mono text-xs text-[var(--text-muted)]" title={formatTime(resource.updated_at || resource.created_at)}>
                    {formatCompactTime(resource.updated_at || resource.created_at)}
                  </td>
                  <td className="px-3 py-2.5 align-top" onClick={(event) => event.stopPropagation()}>
                    <div className="flex items-center justify-end opacity-60 transition-opacity group-hover:opacity-100">
                      <ActionMenu
                        resource={resource}
                        onDetail={() => setDetail(resource)}
                        onAction={handleAction}
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {detail ? (
        <DetailModal
          resource={detail}
          account={detailAccount}
          address={detailAddress}
          link={detailLink}
          onClose={() => setDetail(null)}
        />
      ) : null}

      {mailDialog ? (
        <MailMessagesModal
          resource={mailDialog.resource}
          messages={mailDialog.messages}
          loading={mailDialog.loading}
          error={mailDialog.error}
          onRefresh={() => fetchMessages(mailDialog.resource)}
          onClose={() => setMailDialog(null)}
        />
      ) : null}

      {registerCandidates.length > 0 ? (
        <MailboxRegisterConfigModal
          resources={registerCandidates}
          onStart={startRegisterTasks}
          onClose={() => setRegisterCandidates([])}
        />
      ) : null}

      {registerTask ? (
        <MailboxRegisterTaskModal
          resource={registerTask.resource}
          taskId={registerTask.taskId}
          taskStatus={registerTask.status}
          onDone={(status) => {
            setRegisterTask((current) => current ? { ...current, status } : current);
            load();
          }}
          onClose={() => {
            setRegisterTask(null);
            load();
          }}
        />
      ) : null}
    </div>
  );
}
