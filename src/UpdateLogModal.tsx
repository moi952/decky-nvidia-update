import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { call } from "@decky/api";
import {
  ConfirmModal,
  Focusable,
  GamepadButton,
  type GamepadEvent,
} from "@decky/ui";
import { SiNvidia } from "react-icons/si";
import { useTranslation } from "react-i18next";
import { withTimeout } from "./utils/driverOperation";
import { stickHint, useScrollHint } from "./useScrollHint";
import { ControllerGlyph } from "./ControllerGlyph";
import { useControllerScroll } from "./useControllerScroll";

type LogSnapshot = {
  lines: string[];
  truncated: boolean;
  path: string;
  error: string | null;
};

function focusWithController(element: HTMLElement | null | undefined) {
  if (!element) return;
  element.dispatchEvent(
    new element.ownerDocument.defaultView!.CustomEvent("vgp_requestfocus", {
      bubbles: true,
      cancelable: true,
      detail: { button: 1, source: 0 },
    }),
  );
}

export function UpdateLogModal({ closeModal }: { closeModal?: () => void }) {
  useScrollHint();
  const { t } = useTranslation("panel");
  const [log, setLog] = useState<LogSnapshot | null>(null);
  const [error, setError] = useState(false);
  const [browsing, setBrowsing] = useState(false);
  const [focused, setFocused] = useState(false);
  const viewport = useRef<HTMLDivElement>(null);
  useControllerScroll(viewport, browsing && focused);

  const toggleMode = () => setBrowsing((value) => !value);
  const closeButton = () =>
    viewport.current
      ?.closest(".DialogContent")
      ?.querySelector<HTMLButtonElement>('button[type="submit"]');

  useEffect(() => {
    const doc = viewport.current?.ownerDocument;
    const shortcut = (event: Event) => {
      const e = event as GamepadEvent;
      if (e.detail.button !== GamepadButton.LSTICK_CLICK) return;
      if (!(e.target as Element)?.closest?.(".nvidia-update-log-modal")) return;
      e.preventDefault();
      e.stopPropagation();
      if (e.type === "vgp_onbuttondown" && !e.detail.is_repeat) {
        toggleMode();
        focusWithController(viewport.current);
      }
    };
    // ConfirmModal puts its body and buttons in separate navigation groups.
    // Keep the Close button reachable without scrolling the log with the D-pad.
    const navigate = (event: Event) => {
      const e = event as GamepadEvent;
      const target = e.target as Node | null;
      const close = closeButton();
      const next =
        e.detail.button === GamepadButton.DIR_DOWN &&
        viewport.current?.contains(target)
          ? close
          : e.detail.button === GamepadButton.DIR_UP && close?.contains(target)
            ? viewport.current
            : null;
      if (!next) return;
      e.preventDefault();
      e.stopPropagation();
      focusWithController(next);
    };
    doc?.addEventListener("vgp_onbuttondown", shortcut, true);
    doc?.addEventListener("vgp_onbuttonup", shortcut, true);
    doc?.addEventListener("vgp_ondirection", navigate, true);
    return () => {
      doc?.removeEventListener("vgp_onbuttondown", shortcut, true);
      doc?.removeEventListener("vgp_onbuttonup", shortcut, true);
      doc?.removeEventListener("vgp_ondirection", navigate, true);
    };
  }, []);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const next = await withTimeout(call<[], LogSnapshot>("get_update_log"));
        if (disposed) return;
        setError(Boolean(next.error));
        if (!next.error) {
          setLog((previous) =>
            JSON.stringify(previous) === JSON.stringify(next) ? previous : next,
          );
        }
      } catch {
        if (!disposed) setError(true);
      } finally {
        if (!disposed) timer = setTimeout(refresh, 2000);
      }
    };
    void refresh();
    return () => {
      disposed = true;
      clearTimeout(timer);
    };
  }, []);

  useLayoutEffect(() => {
    if (!browsing && viewport.current)
      viewport.current.scrollTop = viewport.current.scrollHeight;
  }, [log, browsing]);

  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element || browsing) return;
    const observer = new ResizeObserver(() => {
      element.scrollTop = element.scrollHeight;
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [browsing]);

  const move = (distance: number) => {
    if (!viewport.current) return;
    if (browsing) viewport.current.scrollTop += distance;
  };

  return (
    <ConfirmModal
      strTitle={
        <div
          className="nvidia-log-title"
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 24,
          }}
        >
          <span>{t("log_title")}</span>
          <SiNvidia size={34} color="#76b900" aria-label="NVIDIA" />
        </div>
      }
      strOKButtonText={t("close_button")}
      onOK={closeModal}
      onCancel={closeModal}
      bAlertDialog
      bAllowFullSize
      modalClassName="nvidia-update-log-modal"
    >
      <style>{`
        .nvidia-update-log-modal { left: 0; right: 0; width: auto; justify-content: center; }
        .nvidia-update-log-modal .DialogContent { width: min(900px, 90vw); max-width: 90vw; margin-left: auto; margin-right: auto; }
        .nvidia-update-log-modal .DialogContent_InnerWidth { width: 100%; max-width: none; }
        .nvidia-log-title { width: 100%; }
        .nvidia-update-log-modal .DialogHeader:has(.nvidia-log-title) { width: 100%; }
      `}</style>
      <div
        role="status"
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
          marginBottom: 12,
          padding: "10px 14px",
          background: browsing ? "rgba(26,159,255,.14)" : "rgba(118,185,0,.14)",
          borderLeft: `3px solid ${browsing ? "#1a9fff" : "#76b900"}`,
          fontSize: 13,
        }}
      >
        <div>
          <strong style={{ color: browsing ? "#6dc2ff" : "#a4d858" }}>
            {t(browsing ? "log_history_mode" : "log_live_mode")}
          </strong>
          <div style={{ marginTop: 3, opacity: 0.85 }}>
            {t(browsing ? "log_history_hint" : "log_live_hint")}
          </div>
        </div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            flexShrink: 0,
          }}
        >
          <ControllerGlyph click />
          <span>
            {t(browsing ? "log_follow_latest" : "log_browse_history")}
          </span>
        </div>
      </div>
      {error && (
        <div role="status" style={{ color: "#ffcf5c", marginBottom: 8 }}>
          {t("log_unavailable")}
        </div>
      )}
      <Focusable
        ref={viewport}
        data-nvidia-update-log="true"
        role="region"
        aria-label={t("log_title")}
        tabIndex={0}
        data-log-mode={browsing ? "history" : "live"}
        onActivate={toggleMode}
        onClick={toggleMode}
        onOKActionDescription={t(
          browsing ? "log_follow_latest" : "log_browse_history",
        )}
        onGamepadFocus={() => setFocused(true)}
        onGamepadBlur={() => setFocused(false)}
        actionDescriptionMap={{
          [GamepadButton.LSTICK_CLICK]: stickHint(
            t(browsing ? "log_follow_latest" : "log_browse_history"),
          ),
          ...(browsing
            ? { [GamepadButton.RSTICK_TOUCH]: stickHint("Scroll") }
            : {}),
        }}
        onScroll={(event) => {
          const element = event.currentTarget;
          if (!browsing) element.scrollTop = element.scrollHeight;
        }}
        onWheel={(event) => {
          event.preventDefault();
          event.stopPropagation();
          move(event.deltaY);
        }}
        onKeyDown={(event) => {
          if (!browsing && event.key !== "End") {
            if (event.key !== "ArrowDown") return;
            event.preventDefault();
            event.stopPropagation();
            focusWithController(closeButton());
            return;
          }
          const height = event.currentTarget.clientHeight;
          const distances: Record<string, number> = {
            ArrowUp: -48,
            ArrowDown: 48,
            PageUp: -height * 0.8,
            PageDown: height * 0.8,
            Home: -event.currentTarget.scrollHeight,
          };
          if (event.key === "End") setBrowsing(false);
          else if (event.key in distances) move(distances[event.key]);
          else return;
          event.preventDefault();
          event.stopPropagation();
        }}
        onGamepadDirection={(event) => {
          if (!browsing) return;
          if (
            event.detail.button !== GamepadButton.DIR_UP &&
            event.detail.button !== GamepadButton.DIR_DOWN
          )
            return;
          event.preventDefault();
          event.stopPropagation();
        }}
        style={{
          height: "min(50vh, 480px)",
          overflowY: "hidden",
          overscrollBehavior: "contain",
          padding: "12px 16px",
          background: "rgba(0, 0, 0, 0.35)",
          borderRadius: 6,
          fontSize: 13,
          lineHeight: 1.5,
          fontFamily: "monospace",
          whiteSpace: "pre-wrap",
          overflowWrap: "anywhere",
          userSelect: "text",
        }}
      >
        {log
          ? log.lines.length > 0
            ? log.lines.join("\n")
            : t("log_empty")
          : t("log_loading")}
      </Focusable>
      {log?.truncated && (
        <div style={{ fontSize: 11, marginTop: 8 }}>
          {t("log_truncated", { count: log.lines.length })}
        </div>
      )}
      {log?.path && (
        <div
          style={{
            fontSize: 11,
            opacity: 0.6,
            marginTop: 8,
            overflowWrap: "anywhere",
          }}
        >
          {t("full_log_label", { path: log.path })}
        </div>
      )}
    </ConfirmModal>
  );
}
