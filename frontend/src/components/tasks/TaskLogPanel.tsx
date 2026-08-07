import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Square } from "lucide-react";

import { API_BASE, apiFetch } from "@/lib/utils";
import {
  getTaskStatusText,
  isCancellableTaskStatus,
  isTerminalTaskStatus,
} from "@/lib/tasks";
import { useI18n } from "@/lib/i18n-context";

type LogEvent = {
  id: number;
  line: string;
  subtaskId: string;
  subtaskLabel: string;
};

type LogGroup = {
  id: string;
  label: string;
  events: LogEvent[];
};

const MAIN_GROUP_ID = "__main__";
const INITIAL_EVENT_LIMIT = 1000;
const LOAD_OLDER_LIMIT = 500;

function classifyLine(line: string): string {
  if (line.includes("✓") || line.includes("成功")) return "text-emerald-400";
  if (line.includes("✗") || line.includes("失败") || line.includes("错误"))
    return "text-red-400";
  return "text-[var(--text-secondary)]";
}

function toLogEvent(payload: any, fallbackId: number): LogEvent | null {
  if (!payload?.line) return null;
  const detail = payload?.detail || {};
  return {
    id: Number(payload?.id || 0) || fallbackId,
    line: String(payload.line),
    subtaskId: String(detail?.subtask_id || ""),
    subtaskLabel: String(detail?.subtask_label || ""),
  };
}

export function TaskLogPanel({
  taskId,
  onDone,
}: {
  taskId: string;
  onDone: (status: string) => void;
}) {
  const { t, language } = useI18n();
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [task, setTask] = useState<any | null>(null);
  const [doneStatus, setDoneStatus] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [hasMoreBefore, setHasMoreBefore] = useState(false);
  const [followLive, setFollowLive] = useState(true);
  const [loadError, setLoadError] = useState("");

  const seenEventIdsRef = useRef<Set<number>>(new Set());
  const cursorRef = useRef(0);
  const oldestEventIdRef = useRef(0);
  const doneRef = useRef(false);
  const followLiveRef = useRef(true);
  const onDoneRef = useRef(onDone);
  const sseHealthyRef = useRef(false);
  const eventSourceRef = useRef<EventSource | null>(null);
  const logViewportRef = useRef<HTMLDivElement | null>(null);
  const restoreScrollRef = useRef<{ top: number; height: number } | null>(null);

  useEffect(() => {
    onDoneRef.current = onDone;
  }, [onDone]);

  useEffect(() => {
    followLiveRef.current = followLive;
  }, [followLive]);

  const mergeEvents = (
    payloads: any[],
    placement: "append" | "prepend" | "replace" = "append",
  ) => {
    const normalized: LogEvent[] = [];
    for (const payload of payloads) {
      const eventId = Number(payload?.id || 0);
      if (eventId && seenEventIdsRef.current.has(eventId)) continue;
      const event = toLogEvent(payload, eventId || normalized.length + 1);
      if (!event) continue;
      if (eventId) {
        seenEventIdsRef.current.add(eventId);
        cursorRef.current = Math.max(cursorRef.current, eventId);
        oldestEventIdRef.current =
          oldestEventIdRef.current > 0
            ? Math.min(oldestEventIdRef.current, eventId)
            : eventId;
      }
      normalized.push(event);
    }
    if (normalized.length === 0) return;
    setEvents((prev) => {
      if (placement === "replace") return normalized;
      if (placement === "prepend") return [...normalized, ...prev];
      return [...prev, ...normalized];
    });
  };

  useEffect(() => {
    if (!taskId) return;

    let disposed = false;
    let progressPoll = 0;
    let fallbackPoll = 0;

    seenEventIdsRef.current = new Set();
    cursorRef.current = 0;
    oldestEventIdRef.current = 0;
    doneRef.current = false;
    sseHealthyRef.current = false;
    followLiveRef.current = true;
    setEvents([]);
    setTask(null);
    setDoneStatus(null);
    setStopping(false);
    setCollapsed({});
    setHasMoreBefore(false);
    setFollowLive(true);
    setLoadError("");
    setLoadingHistory(true);

    const markDone = (status: string) => {
      if (doneRef.current) return;
      doneRef.current = true;
      sseHealthyRef.current = false;
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
      setDoneStatus(status);
      onDoneRef.current(status);
    };

    const syncTask = async () => {
      const latest = await apiFetch(`/tasks/${taskId}`);
      if (disposed) return latest;
      setTask(latest);
      if (isTerminalTaskStatus(latest.status)) {
        markDone(latest.status);
      }
      return latest;
    };

    const loadInitial = async () => {
      try {
        const [latest, history] = await Promise.all([
          apiFetch(`/tasks/${taskId}`),
          apiFetch(`/tasks/${taskId}/events?latest=true&limit=${INITIAL_EVENT_LIMIT}`),
        ]);
        if (disposed) return;
        setTask(latest);
        setHasMoreBefore(Boolean(history.has_more_before));
        if (Number(history.before || 0) > 0) {
          oldestEventIdRef.current = Number(history.before || 0);
        }
        mergeEvents(history.items || [], "replace");
        if (isTerminalTaskStatus(latest.status)) {
          markDone(latest.status);
          return;
        }

        const es = new EventSource(
          `${API_BASE}/tasks/${taskId}/logs/stream?since=${cursorRef.current}`,
        );
        eventSourceRef.current = es;
        es.onopen = () => {
          sseHealthyRef.current = true;
        };
        es.onmessage = (event) => {
          sseHealthyRef.current = true;
          const payload = JSON.parse(event.data);
          mergeEvents([payload], "append");
          if (payload?.done) {
            markDone(payload.status || "succeeded");
          }
        };
        es.onerror = () => {
          if (doneRef.current) {
            es.close();
            if (eventSourceRef.current === es) {
              eventSourceRef.current = null;
            }
            return;
          }
          sseHealthyRef.current = false;
        };

        progressPoll = window.setInterval(() => {
          if (doneRef.current) return;
          syncTask().catch(() => {});
        }, 1500);

        fallbackPoll = window.setInterval(async () => {
          if (doneRef.current || sseHealthyRef.current) return;
          try {
            const data = await apiFetch(
              `/tasks/${taskId}/events?since=${cursorRef.current}&limit=500`,
            );
            mergeEvents(data.items || [], "append");
          } catch {
            // passive
          }
        }, 1000);
      } catch (exc: any) {
        if (!disposed) {
          setLoadError(exc?.message || t("taskLog.loadFailed"));
        }
      } finally {
        if (!disposed) setLoadingHistory(false);
      }
    };

    loadInitial();

    return () => {
      disposed = true;
      sseHealthyRef.current = false;
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
      window.clearInterval(progressPoll);
      window.clearInterval(fallbackPoll);
    };
  }, [taskId, t]);

  useLayoutEffect(() => {
    const viewport = logViewportRef.current;
    if (!viewport) return;
    const restore = restoreScrollRef.current;
    if (restore) {
      restoreScrollRef.current = null;
      viewport.scrollTop = viewport.scrollHeight - restore.height + restore.top;
      return;
    }
    if (followLiveRef.current) {
      viewport.scrollTop = viewport.scrollHeight;
      requestAnimationFrame(() => {
        if (!followLiveRef.current) return;
        viewport.scrollTop = viewport.scrollHeight;
      });
    }
  }, [events.length]);

  const groups: LogGroup[] = useMemo(() => {
    const map = new Map<string, LogGroup>();
    map.set(MAIN_GROUP_ID, {
      id: MAIN_GROUP_ID,
      label: t("taskLog.mainGroup"),
      events: [],
    });
    for (const ev of events) {
      const key = ev.subtaskId || MAIN_GROUP_ID;
      if (!map.has(key)) {
        map.set(key, {
          id: key,
          label: ev.subtaskLabel || key,
          events: [],
        });
      }
      const group = map.get(key)!;
      group.events.push(ev);
      if (key !== MAIN_GROUP_ID && ev.subtaskLabel) {
        group.label = ev.subtaskLabel;
      }
    }
    return Array.from(map.values());
  }, [events, t]);

  const toggleGroup = (id: string) => {
    setCollapsed((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const handleLogScroll = () => {
    const viewport = logViewportRef.current;
    if (!viewport) return;
    const distanceFromBottom =
      viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight;
    const nextFollow = distanceFromBottom < 48;
    if (nextFollow !== followLiveRef.current) {
      followLiveRef.current = nextFollow;
      setFollowLive(nextFollow);
    }
  };

  const jumpToLatest = () => {
    const viewport = logViewportRef.current;
    followLiveRef.current = true;
    setFollowLive(true);
    if (viewport) {
      viewport.scrollTop = viewport.scrollHeight;
      requestAnimationFrame(() => {
        viewport.scrollTop = viewport.scrollHeight;
      });
    }
  };

  const loadOlder = async () => {
    if (loadingOlder || !hasMoreBefore || oldestEventIdRef.current <= 0) return;
    const viewport = logViewportRef.current;
    if (viewport) {
      restoreScrollRef.current = {
        top: viewport.scrollTop,
        height: viewport.scrollHeight,
      };
    }
    setLoadingOlder(true);
    try {
      const data = await apiFetch(
        `/tasks/${taskId}/events?before=${oldestEventIdRef.current}&limit=${LOAD_OLDER_LIMIT}`,
      );
      setHasMoreBefore(Boolean(data.has_more_before));
      if (Number(data.before || 0) > 0) {
        oldestEventIdRef.current = Number(data.before || 0);
      }
      mergeEvents(data.items || [], "prepend");
    } finally {
      setLoadingOlder(false);
    }
  };

  const currentStatus = doneStatus || task?.status || "running";
  const canStop = Boolean(task?.cancellable || isCancellableTaskStatus(currentStatus));
  const progress = task?.progress_detail || {};
  const progressTotal = Number(progress.total || 0);
  const progressCurrent = Number(progress.current || 0);
  const progressPercent =
    progressTotal > 0
      ? Math.min(100, Math.round((progressCurrent / progressTotal) * 100))
      : 0;
  const errorText =
    task?.error || (Array.isArray(task?.errors) ? task.errors[0] : "");
  const statusTone =
    currentStatus === "succeeded"
      ? "border-emerald-400/40 bg-emerald-400/10 text-emerald-200"
      : currentStatus === "failed"
        ? "border-red-400/40 bg-red-400/10 text-red-200"
        : currentStatus === "cancelled" || currentStatus === "interrupted"
          ? "border-amber-400/40 bg-amber-400/10 text-amber-200"
          : "border-sky-400/40 bg-sky-400/10 text-sky-200";

  const copyLogs = () => {
    navigator.clipboard
      ?.writeText(events.map((ev) => ev.line).join("\n"))
      .catch(() => {});
  };

  const stopTask = async () => {
    if (!taskId || stopping || isTerminalTaskStatus(currentStatus)) return;
    setStopping(true);
    try {
      const latest = await apiFetch(`/tasks/${taskId}/cancel`, { method: "POST" });
      setTask(latest);
    } finally {
      setStopping(false);
    }
  };

  return (
    <div className="flex min-h-0 flex-col gap-4">
      <div className="grid gap-3 md:grid-cols-3">
        <div className={`rounded-2xl border px-4 py-3 ${statusTone}`}>
          <div className="text-[11px] uppercase tracking-[0.18em] opacity-70">
            {t("taskLog.status")}
          </div>
          <div className="mt-1 text-sm font-semibold">
            {getTaskStatusText(currentStatus, language)}
          </div>
        </div>
        <div className="rounded-2xl border border-[var(--border)] bg-[var(--bg-hover)] px-4 py-3">
          <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">
            {t("taskLog.progress")}
          </div>
          <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">
            {progress.label || task?.progress || "0/0"}
          </div>
        </div>
        <div className="rounded-2xl border border-[var(--border)] bg-[var(--bg-hover)] px-4 py-3">
          <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">
            {t("taskLog.events")}
          </div>
          <div className="mt-1 text-sm font-semibold text-[var(--text-primary)]">
            {t("taskLog.logCount", { count: events.length })}
          </div>
        </div>
      </div>

      <div className="h-2 overflow-hidden rounded-full bg-[var(--bg-hover)] ring-1 ring-[var(--border)]">
        <div
          className={`h-full rounded-full transition-all duration-500 ${
            currentStatus === "failed"
              ? "bg-red-400"
              : currentStatus === "succeeded"
                ? "bg-emerald-400"
                : "bg-sky-400"
          }`}
          style={{
            width: `${progressTotal > 0 ? progressPercent : isTerminalTaskStatus(currentStatus) ? 100 : 18}%`,
          }}
        />
      </div>

      {errorText ? (
        <div className="rounded-2xl border border-red-400/35 bg-red-500/10 px-4 py-3 text-sm text-red-100">
          <div className="mb-1 font-semibold">
            {t("taskLog.failureReason")}
          </div>
          <div className="break-words text-red-100/85">{errorText}</div>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">
            {t("taskLog.liveLog")}
          </div>
          <div className="mt-1 text-sm font-medium text-[var(--text-primary)]">
            {t("taskLog.liveTitle")}
          </div>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {!followLive && (
            <button
              type="button"
              onClick={jumpToLatest}
              className="rounded-full border border-[var(--accent-edge)] bg-[var(--accent-soft)] px-3 py-1.5 text-xs text-[var(--accent)] hover:text-[var(--accent-strong)]"
            >
              {t("taskLog.jumpLatest")}
            </button>
          )}
          {canStop && !isTerminalTaskStatus(currentStatus) ? (
            <button
              type="button"
              onClick={stopTask}
              disabled={stopping || currentStatus === "cancel_requested"}
              className="inline-flex items-center rounded-full border border-amber-400/30 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-300 hover:text-amber-200 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <Square className="mr-1 h-3.5 w-3.5" />
              {stopping || currentStatus === "cancel_requested"
                ? t("taskLog.stopping")
                : t("taskLog.stopTask")}
            </button>
          ) : null}
          <button
            type="button"
            onClick={copyLogs}
            className="rounded-full border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-1.5 text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
          >
            {t("taskLog.copyLogs")}
          </button>
        </div>
      </div>

      <div
        ref={logViewportRef}
        onScroll={handleLogScroll}
        className="h-[min(52vh,560px)] min-h-[260px] overflow-y-auto rounded-xl border border-[var(--border)] bg-[var(--bg-input)] p-3 font-mono text-xs"
      >
        {loadError ? (
          <div className="rounded-lg border border-red-400/30 bg-red-500/10 px-3 py-2 text-red-300">
            {loadError}
          </div>
        ) : null}
        {hasMoreBefore ? (
          <div className="mb-2 flex justify-center">
            <button
              type="button"
              onClick={loadOlder}
              disabled={loadingOlder}
              className="rounded-full border border-[var(--border)] bg-[var(--bg-hover)] px-3 py-1.5 text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] disabled:opacity-60"
            >
              {loadingOlder ? t("taskLog.loadingOlder") : t("taskLog.loadOlder")}
            </button>
          </div>
        ) : null}
        {events.length === 0 ? (
          <div className="flex h-full min-h-[180px] items-center justify-center rounded-2xl border border-dashed border-[var(--border)] text-[var(--text-muted)]">
            {loadingHistory ? t("taskLog.loadingHistory") : t("taskLog.waiting")}
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {groups.map((group) => {
              if (group.id === MAIN_GROUP_ID && group.events.length === 0) {
                return null;
              }
              return (
                <LogGroupView
                  key={group.id}
                  group={group}
                  collapsed={!!collapsed[group.id]}
                  isMain={group.id === MAIN_GROUP_ID}
                  onToggle={() => toggleGroup(group.id)}
                />
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function LogGroupView({
  group,
  collapsed,
  isMain,
  onToggle,
}: {
  group: LogGroup;
  collapsed: boolean;
  isMain: boolean;
  onToggle: () => void;
}) {
  const { t } = useI18n();

  return (
    <div className="overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-pane)]/40">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-2 border-b border-[var(--border)] bg-[var(--bg-hover)]/60 px-3 py-1.5 text-left text-[11px] uppercase tracking-[0.16em] text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
      >
        {collapsed ? (
          <ChevronRight className="h-3.5 w-3.5" />
        ) : (
          <ChevronDown className="h-3.5 w-3.5" />
        )}
        <span className="truncate">
          {isMain ? t("taskLog.mainGroup") : group.label}
        </span>
        <span className="ml-auto text-[10px] text-[var(--text-muted)]">
          {t("taskLog.logCount", { count: group.events.length })}
        </span>
      </button>
      {!collapsed && (
        <div className="px-2 py-2">
          <div className="space-y-1">
            {group.events.map((ev) => (
              <div
                key={ev.id}
                className={`rounded-md border border-white/5 bg-white/[0.025] px-3 py-1.5 leading-5 ${classifyLine(ev.line)}`}
              >
                {ev.line}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
