import { useCallback, useEffect, useMemo, useState } from "react";
import { Eye, ListChecks, RefreshCw, Square, X } from "lucide-react";

import { TaskLogPanel } from "@/components/tasks/TaskLogPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatDateTime, type Language } from "@/lib/i18n";
import { useI18n } from "@/lib/i18n-context";
import type { TranslationKey } from "@/lib/i18n";
import {
  getTaskStatusText,
  isCancellableTaskStatus,
  TASK_STATUS_VARIANTS,
} from "@/lib/tasks";
import { apiFetch, cn } from "@/lib/utils";

type TaskItem = {
  id: string;
  task_id?: string;
  type: string;
  platform: string;
  status: string;
  progress: string;
  progress_detail?: {
    current: number;
    total: number;
    label: string;
  };
  success: number;
  error_count: number;
  error: string;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
  updated_at?: string;
};

const PAGE_SIZE = 25;

const STATUS_OPTIONS = [
  "",
  "pending",
  "claimed",
  "running",
  "succeeded",
  "failed",
  "interrupted",
  "cancel_requested",
  "cancelled",
];

const TYPE_OPTIONS = [
  "",
  "register",
  "account_check_all",
  "platform_action",
  "codex_oauth_batch",
  "relogin_batch",
  "account_push",
];

function taskTypeLabel(type: string, t: (key: TranslationKey) => string) {
  switch (type) {
    case "register":
      return t("taskHistory.type.register");
    case "account_check_all":
      return t("taskHistory.type.account_check_all");
    case "platform_action":
      return t("taskHistory.type.platform_action");
    case "codex_oauth_batch":
      return t("taskHistory.type.codex_oauth_batch");
    case "relogin_batch":
      return t("taskHistory.type.relogin_batch");
    case "account_push":
      return t("taskHistory.type.account_push");
    default:
      return type || "-";
  }
}

function formatMaybeDate(value: string | undefined, language: Language) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return formatDateTime(date, language, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function progressPercent(task: TaskItem) {
  const detail = task.progress_detail;
  if (!detail || !detail.total) return task.status === "succeeded" ? 100 : 0;
  return Math.max(0, Math.min(100, Math.round((detail.current / detail.total) * 100)));
}

export default function TaskHistory() {
  const { t, language } = useI18n();
  const [items, setItems] = useState<TaskItem[]>([]);
  const [total, setTotal] = useState(0);
  const [running, setRunning] = useState(0);
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState("");
  const [platform, setPlatform] = useState("");
  const [type, setType] = useState("");
  const [loading, setLoading] = useState(false);
  const [terminatingId, setTerminatingId] = useState("");
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(offset),
      });
      if (status) params.set("status", status);
      if (platform) params.set("platform", platform);
      if (type) params.set("type", type);
      const data = await apiFetch(`/tasks?${params.toString()}`);
      setItems(data.items || []);
      setTotal(Number(data.total || 0));
      setRunning(Number(data.running || 0));
    } catch (exc: any) {
      setError(exc?.message || t("taskHistory.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [offset, platform, status, t, type]);

  useEffect(() => {
    load();
  }, [load]);

  const platforms = useMemo(() => {
    const values = new Set(items.map((item) => item.platform).filter(Boolean));
    if (platform) values.add(platform);
    return Array.from(values).sort();
  }, [items, platform]);

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  const cancelTask = async (taskId: string) => {
    setTerminatingId(taskId);
    try {
      await apiFetch(`/tasks/${taskId}/cancel`, { method: "POST" });
      await load();
    } finally {
      setTerminatingId("");
    }
  };

  const resetFilters = () => {
    setStatus("");
    setPlatform("");
    setType("");
    setOffset(0);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-[var(--text-primary)]">
            {t("taskHistory.title")}
          </h1>
          <p className="mt-1 text-sm text-[var(--text-muted)]">
            {t("taskHistory.subtitle")}
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          <RefreshCw className={cn("mr-1.5 h-4 w-4", loading && "animate-spin")} />
          {t("common.refresh")}
        </Button>
      </div>

      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-3">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-[11px] tracking-[0.16em] text-[var(--text-muted)]">
                {t("taskHistory.metric.total")}
              </p>
              <p className="mt-1 text-xl font-semibold text-[var(--text-primary)]">
                {total}
              </p>
            </div>
            <div className="flex h-9 w-9 items-center justify-center rounded-md border border-[var(--border-soft)] bg-[var(--chip-bg)]">
              <ListChecks className="h-4.5 w-4.5 text-[var(--text-accent)]" />
            </div>
          </div>
        </div>
        <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-3">
          <p className="text-[11px] tracking-[0.16em] text-[var(--text-muted)]">
            {t("taskHistory.metric.running")}
          </p>
          <p className="mt-1 text-xl font-semibold text-[var(--text-primary)]">
            {running}
          </p>
        </div>
      </div>

      <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)]">
        <div className="flex flex-wrap items-center gap-2 border-b border-[var(--border)] px-3 py-3">
          <select
            className="control-surface control-surface-compact w-auto min-w-36"
            value={status}
            onChange={(event) => {
              setStatus(event.target.value);
              setOffset(0);
            }}
          >
            {STATUS_OPTIONS.map((value) => (
              <option key={value || "all"} value={value}>
                {value ? getTaskStatusText(value, language) : t("taskHistory.allStatuses")}
              </option>
            ))}
          </select>
          <select
            className="control-surface control-surface-compact w-auto min-w-36"
            value={type}
            onChange={(event) => {
              setType(event.target.value);
              setOffset(0);
            }}
          >
            {TYPE_OPTIONS.map((value) => (
              <option key={value || "all"} value={value}>
                {value ? taskTypeLabel(value, t) : t("taskHistory.allTypes")}
              </option>
            ))}
          </select>
          <select
            className="control-surface control-surface-compact w-auto min-w-36"
            value={platform}
            onChange={(event) => {
              setPlatform(event.target.value);
              setOffset(0);
            }}
          >
            <option value="">{t("taskHistory.allPlatforms")}</option>
            {platforms.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          {(status || platform || type) && (
            <Button variant="ghost" size="sm" onClick={resetFilters}>
              {t("common.clear")}
            </Button>
          )}
        </div>

        {error && (
          <div className="border-b border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-400">
            {error}
          </div>
        )}

        <div className="glass-table-wrap">
          <table className="w-full min-w-[900px] text-sm">
            <thead>
              <tr className="border-b border-[var(--border)] text-[var(--text-muted)]">
                <th className="px-3 py-2 text-left">{t("taskHistory.taskId")}</th>
                <th className="px-3 py-2 text-left">{t("taskHistory.type")}</th>
                <th className="px-3 py-2 text-left">{t("common.platform")}</th>
                <th className="px-3 py-2 text-left">{t("common.status")}</th>
                <th className="px-3 py-2 text-left">{t("common.progress")}</th>
                <th className="px-3 py-2 text-left">{t("taskHistory.successFailure")}</th>
                <th className="px-3 py-2 text-left">{t("common.date")}</th>
                <th className="px-3 py-2 text-right">{t("common.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((task) => {
                const percent = progressPercent(task);
                return (
                  <tr
                    key={task.id}
                    className="border-b border-[var(--border-soft)] hover:bg-[var(--bg-hover)]"
                  >
                    <td className="px-3 py-3 align-top">
                      <div className="max-w-[220px] truncate font-mono text-xs text-[var(--text-secondary)]">
                        {task.id}
                      </div>
                      {task.error && (
                        <div className="mt-1 max-w-[220px] truncate text-xs text-red-400">
                          {task.error}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-3 align-top text-[var(--text-secondary)]">
                      {taskTypeLabel(task.type, t)}
                    </td>
                    <td className="px-3 py-3 align-top text-[var(--text-secondary)]">
                      {task.platform || "-"}
                    </td>
                    <td className="px-3 py-3 align-top">
                      <Badge variant={TASK_STATUS_VARIANTS[task.status] || "secondary"}>
                        {getTaskStatusText(task.status, language)}
                      </Badge>
                    </td>
                    <td className="px-3 py-3 align-top">
                      <div className="min-w-32">
                        <div className="flex items-center justify-between gap-2 text-xs text-[var(--text-muted)]">
                          <span>{task.progress_detail?.label || task.progress || "0/0"}</span>
                          <span>{percent}%</span>
                        </div>
                        <div className="progress-track mt-2">
                          <div className="progress-fill" style={{ width: `${percent}%` }} />
                        </div>
                      </div>
                    </td>
                    <td className="px-3 py-3 align-top text-[var(--text-secondary)]">
                      {task.success} / {task.error_count}
                    </td>
                    <td className="px-3 py-3 align-top text-xs text-[var(--text-muted)]">
                      {formatMaybeDate(task.created_at, language)}
                    </td>
                    <td className="px-3 py-3 align-top">
                      <div className="flex justify-end gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => setSelectedTaskId(task.id)}
                        >
                          <Eye className="mr-1 h-3.5 w-3.5" />
                          {t("taskHistory.viewLogs")}
                        </Button>
                        {isCancellableTaskStatus(task.status) && (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => cancelTask(task.id)}
                            disabled={terminatingId === task.id}
                            className="text-amber-500 hover:text-amber-400"
                          >
                            <Square className="mr-1 h-3.5 w-3.5" />
                            {terminatingId === task.id
                              ? t("taskHistory.terminating")
                              : t("taskHistory.terminate")}
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {!loading && items.length === 0 && (
          <div className="empty-state-panel m-3">{t("taskHistory.empty")}</div>
        )}
        {loading && items.length === 0 && (
          <div className="empty-state-panel m-3">{t("common.loading")}</div>
        )}

        <div className="flex items-center justify-between gap-3 border-t border-[var(--border)] px-3 py-3 text-xs text-[var(--text-muted)]">
          <span>
            {t("taskHistory.page", { current: currentPage, total: pages })}
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={offset <= 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              {t("taskHistory.prevPage")}
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              {t("taskHistory.nextPage")}
            </Button>
          </div>
        </div>
      </div>

      {selectedTaskId && (
        <div className="dialog-backdrop" role="dialog" aria-modal="true">
          <div className="dialog-panel dialog-panel-lg">
            <div className="flex items-center justify-between gap-3 border-b border-[var(--border)] px-4 py-3">
              <div>
                <h2 className="text-base font-semibold text-[var(--text-primary)]">
                  {t("taskHistory.logTitle")}
                </h2>
                <p className="mt-0.5 font-mono text-xs text-[var(--text-muted)]">
                  {selectedTaskId}
                </p>
              </div>
              <button
                onClick={() => setSelectedTaskId("")}
                className="rounded-md p-2 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
                title={t("common.close")}
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="max-h-[calc(100dvh-150px)] overflow-y-auto p-4">
              <TaskLogPanel
                taskId={selectedTaskId}
                onDone={() => {
                  load();
                }}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
