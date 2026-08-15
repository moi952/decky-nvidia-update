import { useEffect, useRef, useState } from "react";
import {
  ButtonItem,
  Navigation,
  PanelSectionRow,
  ProgressBarWithInfo,
} from "@decky/ui";
import { toaster } from "@decky/api";
import { useTranslation } from "react-i18next";
import { FaArrowCircleUp } from "react-icons/fa";
import { CollapsibleSection } from "./CollapsibleSection";

// current/latest are plain "X.Y.Z" (no leading 'v') so they compare/display
// the same way as everywhere else in this plugin. asset_url + sha256 are
// only ever populated when a real update is available and its release has
// a usable zip asset — see plugin_updater.py.
export type PluginUpdateInfo = {
  current_version: string;
  latest_version: string;
  has_update: boolean;
  release_url: string;
  asset_url: string;
  sha256: string;
  plugin_display_name: string;
  checked_ok: boolean;
};

// Decky Loader's own PluginInstallType enum (backend enums.py) — this
// plugin only ever offers "Update to latest" (the button only appears when
// has_update is already true), so REINSTALL/DOWNGRADE are never needed.
const INSTALL_TYPE_UPDATE = 2;

// window.DeckyBackend lives on whichever window actually created this
// document. In Gaming Mode the Quick Access panel renders inside a popup
// window (opened via window.open by Big Picture Mode) — DeckyBackend is
// undefined on that popup's own `window` there, but reachable via
// `window.opener`.
const getDeckyBackend = (): Window["DeckyBackend"] | null =>
  window.DeckyBackend ?? window.opener?.DeckyBackend ?? null;

// If Decky's own loader install dies silently (e.g. a dead asset URL),
// nothing else would ever flip the "installing" state back off — this is
// an inactivity reset (re-armed on every progress tick), not a single fixed
// deadline, so a legitimately slow download isn't falsely flagged.
const INSTALL_WATCHDOG_TIMEOUT_MS = 45_000;

interface PluginUpdateBannerProps {
  info: PluginUpdateInfo | null;
}

// Small top-of-panel notice, separate from the collapsible section below —
// stays visible without the user needing to expand anything, and renders
// nothing until an update is actually confirmed.
export function PluginUpdateBanner({ info }: PluginUpdateBannerProps) {
  const { t } = useTranslation();
  if (!info?.has_update) return null;
  return (
    <PanelSectionRow>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          fontSize: 12,
          color: "#66c0f4",
          marginBottom: 8,
        }}
      >
        <FaArrowCircleUp />{" "}
        {t("plugin_update_banner", { version: info.latest_version })}
      </div>
    </PanelSectionRow>
  );
}

interface PluginUpdateSectionProps {
  info: PluginUpdateInfo | null;
  checking: boolean;
  expanded: boolean;
  onToggle: () => void;
  onCheckNow: () => void;
}

// Collapsed-by-default details section, meant to sit as the last row of the
// panel (mirrors the channel-filter section's own collapsible style):
// current/latest version, a real one-click "Update now" that hands off to
// Decky Loader's own installer (same route the Decky plugin store itself
// uses), and a link to the release page.
export function PluginUpdateSection({
  info,
  checking,
  expanded,
  onToggle,
  onCheckNow,
}: PluginUpdateSectionProps) {
  const { t } = useTranslation();
  const [installing, setInstalling] = useState(false);
  const [downloadActive, setDownloadActive] = useState(false);
  const [downloadPercent, setDownloadPercent] = useState(0);
  const downloadActiveRef = useRef(false);
  const watchdogRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Mirrors Decky's own loader install progress — the install_plugin call
  // below only registers the request (Decky pops its own confirm modal and
  // does the actual download/extract), so this is how we know it's moving.
  useEffect(() => {
    const backend = getDeckyBackend();
    const name = info?.plugin_display_name;
    if (!backend || !name) return;

    const clearWatchdog = () => {
      if (watchdogRef.current !== null) {
        clearTimeout(watchdogRef.current);
        watchdogRef.current = null;
      }
    };
    const armWatchdog = () => {
      clearWatchdog();
      watchdogRef.current = setTimeout(() => {
        if (!downloadActiveRef.current) return;
        downloadActiveRef.current = false;
        setDownloadActive(false);
        setInstalling(false);
        toaster.toast({
          title: t("plugin_update_install_failed_title"),
          body: t("plugin_update_install_timeout"),
        });
      }, INSTALL_WATCHDOG_TIMEOUT_MS);
    };

    const onStart = (eventName: string) => {
      if (eventName !== name) return;
      downloadActiveRef.current = true;
      setDownloadActive(true);
      setDownloadPercent(0);
      armWatchdog();
    };
    const onInfo = (percent: number) => {
      if (!downloadActiveRef.current) return;
      setDownloadPercent(percent);
      armWatchdog();
    };
    const onFinish = (eventName: string) => {
      if (eventName !== name) return;
      downloadActiveRef.current = false;
      setDownloadPercent(100);
      setDownloadActive(false);
      setInstalling(false);
      clearWatchdog();
    };

    backend.addEventListener("loader/plugin_download_start", onStart);
    backend.addEventListener("loader/plugin_download_info", onInfo);
    backend.addEventListener("loader/plugin_download_finish", onFinish);
    return () => {
      backend.removeEventListener("loader/plugin_download_start", onStart);
      backend.removeEventListener("loader/plugin_download_info", onInfo);
      backend.removeEventListener("loader/plugin_download_finish", onFinish);
      clearWatchdog();
    };
  }, [info?.plugin_display_name, t]);

  const onUpdateNow = async () => {
    if (!info?.has_update || !info.asset_url) return;
    const backend = getDeckyBackend();
    if (!backend) {
      toaster.toast({
        title: t("plugin_update_install_failed_title"),
        body: t("plugin_update_no_backend"),
      });
      return;
    }
    setInstalling(true);
    try {
      // Only registers the request and pops Decky's own native confirm
      // modal (which owns the actual download/install and its own progress
      // bar) — returns immediately, the listeners above mirror the rest.
      await backend.call(
        "utilities/install_plugin",
        info.asset_url,
        info.plugin_display_name,
        info.latest_version,
        info.sha256 || "",
        INSTALL_TYPE_UPDATE,
      );
    } catch (e) {
      setInstalling(false);
      toaster.toast({
        title: t("plugin_update_install_failed_title"),
        body: e instanceof Error ? e.message : String(e),
      });
    }
  };

  // Navigation.NavigateToExternalWeb opens Steam's own browser overlay —
  // unlike a plain <a target="_blank">, it's reachable through the
  // gamepad-driven focus system the rest of this UI relies on.
  const onViewRelease = () => {
    if (info?.release_url) Navigation.NavigateToExternalWeb(info.release_url);
  };

  const busy = installing || downloadActive || checking;

  return (
    <PanelSectionRow>
      <CollapsibleSection
        label={t("plugin_update_section_label")}
        expanded={expanded}
        onToggle={onToggle}
      >
        <PanelSectionRow>
          <div style={{ fontSize: 12, opacity: 0.85, marginTop: 4 }}>
            {t("plugin_update_current", {
              version: info?.current_version || "?",
            })}
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={{ fontSize: 12, opacity: 0.85, marginBottom: 4 }}>
            {!info || !info.checked_ok
              ? t("plugin_update_check_failed")
              : info.has_update
                ? t("plugin_update_latest", { version: info.latest_version })
                : t("plugin_update_up_to_date")}
          </div>
        </PanelSectionRow>

        {(downloadActive || installing) && (
          <PanelSectionRow>
            <ProgressBarWithInfo
              layout="inline"
              bottomSeparator="none"
              nProgress={downloadPercent}
              sOperationText={
                downloadActive
                  ? t("plugin_update_downloading")
                  : t("plugin_update_installing")
              }
            />
          </PanelSectionRow>
        )}

        {info?.has_update && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={onUpdateNow}
              disabled={busy || !info.asset_url}
            >
              {installing || downloadActive
                ? t("plugin_update_installing")
                : t("plugin_update_install_button", {
                    version: info.latest_version,
                  })}
            </ButtonItem>
          </PanelSectionRow>
        )}

        {info?.release_url && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={onViewRelease} disabled={busy}>
              {t("plugin_update_view_release")}
            </ButtonItem>
          </PanelSectionRow>
        )}

        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onCheckNow} disabled={busy}>
            {checking
              ? t("plugin_update_checking")
              : t("plugin_update_check_button")}
          </ButtonItem>
        </PanelSectionRow>
      </CollapsibleSection>
    </PanelSectionRow>
  );
}
