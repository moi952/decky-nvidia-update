import { useEffect, useRef, useState } from "react";
import {
  definePlugin,
  call,
  addEventListener,
  removeEventListener,
} from "@decky/api";
import { pluginToast } from "./utils/toast";
import {
  launchErrorTitle,
  requestOperation,
  withTimeout,
} from "./utils/driverOperation";
import type { LaunchResult } from "./utils/driverOperation";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  ToggleField,
  ConfirmModal,
  showModal,
  staticClasses,
} from "@decky/ui";
import { ActionButton, AnchoredDropdown } from "@moi952/decky-ui-kit";
import {
  PluginUpdateProvider,
  usePluginUpdate,
  pluginUpdateFocus,
  OtherPluginsProvider,
  otherPluginsFocus,
  WhatsNewProvider,
  featureRequestFocus,
  PluginUpdateBanner,
  WhatsNewBanner,
  OtherPluginsBanner,
  getWhatsNewVersions,
} from "@moi952/decky-plugin-toolkit";
import type { PluginUpdateInfo } from "@moi952/decky-plugin-toolkit";
import { useTranslation } from "react-i18next";
import i18n from "i18next";
import { loadTranslations } from "./i18n";
import {
  VersionDropdown,
  compareDriverVersions,
  computeChannelLatest,
  channelCounts,
} from "./VersionDropdown";
import type {
  NvidiaVersionMap,
  ChannelFilters,
  DefaultChannelChoice,
} from "./VersionDropdown";
import { ReleaseNotesModal } from "./ReleaseNotesModal";
import { UpdateLogModal } from "./UpdateLogModal";
import { HelpIndex, helpProps } from "./ContextHelp";
import { SeparatedBlock } from "./SeparatedBlock";
import { CollapsibleSection } from "./CollapsibleSection";
import type { CustomBuild } from "./CustomBuildSection";
import { SettingsView } from "./views/SettingsView";
import { SELF_PLUGIN_ID } from "./utils/otherPlugins";
import { CURRENT_VERSION } from "./utils/githubReleases";
import { SiNvidia } from "react-icons/si";
import {
  FaCheckCircle,
  FaTimesCircle,
  FaArrowCircleUp,
  FaInfoCircle,
  FaPowerOff,
  FaCog,
} from "react-icons/fa";

type UpdateStatus = {
  action?: string;
  pending_reboot?: boolean;
  running: boolean;
  step: number;
  total_steps: number;
  message: string;
  log: string[];
  done: boolean;
  success: boolean | null;
  error: string | null;
  // Set only for specific, recognized failures (currently just the Arch
  // server being unreachable) — lets the UI show a proper localized
  // message instead of relaying the script's raw (English-only) text.
  error_code: string | null;
  driver_spec: string | null;
};

const IDLE_STATUS: UpdateStatus = {
  running: false,
  step: 0,
  total_steps: 8,
  message: "",
  log: [],
  done: false,
  success: null,
  error: null,
  error_code: null,
  driver_spec: null,
  pending_reboot: false,
};

// Mirrors bin/steamos-nvidia-build.sh's own state shape (see
// custom_build.py's DEFAULT_BUILD_STATE) — a separate, smaller status than
// UpdateStatus above since the build script has no error_code/driver_spec
// concept of its own.
type BuildStatus = {
  running: boolean;
  step: number;
  total_steps: number;
  message: string;
  log: string[];
  done: boolean;
  success: boolean | null;
  error: string | null;
  version: string | null;
};

const IDLE_BUILD_STATUS: BuildStatus = {
  running: false,
  step: 0,
  total_steps: 8,
  message: "",
  log: [],
  done: false,
  success: null,
  error: null,
  version: null,
};

type CheckResult = {
  checked_ok: boolean;
  buildable: boolean;
  missing_files: string[];
};

type RepoInfo = {
  stable: string;
  with_testing: string;
  testing_versions: string[];
};

type Settings = {
  show_build_numbers: boolean;
  default_channel: DefaultChannelChoice;
  channel_filters_expanded: boolean;
};

type RootfsSpaceInfo = {
  available: boolean;
  error: string;
  required_mb: number;
  total_mb: number;
  avail_mb: number;
  undersized: boolean;
};

const IDLE_REPO_INFO: RepoInfo = {
  stable: "",
  with_testing: "",
  testing_versions: [],
};

const IDLE_NVIDIA_VERSIONS: NvidiaVersionMap = {};

// Decky/Steam remounts this whole panel on certain interactions (confirmed
// earlier via mount-id logging — picking a dropdown option is one trigger).
// Normal useState resets on that remount; these two module-level caches
// survive it (same trick as VersionDropdown's own lastSelected), so a
// remount mid-session doesn't momentarily null out the fetched NVIDIA
// version data or silently revert the user's channel-filter picks back to
// their defaults — both of which produced visibly wrong/confusing behavior
// (a version losing its "(NFB)" label, or a filter re-checking itself).
let cachedNvidiaVersions: NvidiaVersionMap | null = null;
let cachedBuildableVersions: string[] | null = null;
let cachedChannelFilters: ChannelFilters | null = null;

function Content() {
  const { t } = useTranslation("panel");
  const [currentVersion, setCurrentVersion] = useState<string>("");
  const [repoInfo, setRepoInfo] = useState<RepoInfo>(IDLE_REPO_INFO);
  const [defaultChannel, setDefaultChannel] =
    useState<DefaultChannelChoice>("all");
  const [versions, setVersions] = useState<string[]>([]);
  const [detailedVersions, setDetailedVersions] = useState<string[]>([]);
  const [showBuildNumbers, setShowBuildNumbers] = useState<boolean>(false);
  // Set by VersionDropdown's onSelectionChange — that component owns the
  // actual selection state/UI entirely; this is just the last value it
  // reported up, used for the install button and selectedIsInstalled below.
  const [chosenVersion, setChosenVersion] = useState<string>("");
  const [loadingVersions, setLoadingVersions] = useState<boolean>(true);
  const [status, setStatus] = useState<UpdateStatus>(IDLE_STATUS);
  const [statusLoaded, setStatusLoaded] = useState(false);
  const statusEpoch = useRef(0);
  const [requestPending, setRequestPending] = useState(false);
  const [nvidiaVersions, setNvidiaVersions] = useState<NvidiaVersionMap>(
    () => cachedNvidiaVersions ?? IDLE_NVIDIA_VERSIONS,
  );
  const [showChannelFilters, setShowChannelFilters] = useState<boolean>(true);
  // Collapsed by default — managing already-cached custom builds is an
  // occasional action, not something to surface expanded on every open the
  // way the version picker itself is.
  const [customBuilds, setCustomBuilds] = useState<CustomBuild[]>([]);
  const refreshCustomBuilds = () =>
    call<[], CustomBuild[]>("list_custom_builds").then(setCustomBuilds);
  // Every NVIDIA-published version (>= 590), newest first — merged straight
  // into VersionDropdown's own list so picking one Arch hasn't packaged and
  // hitting Install builds it first, automatically, instead of needing a
  // separate build step.
  const [buildableVersions, setBuildableVersions] = useState<string[]>(
    () => cachedBuildableVersions ?? [],
  );
  const [loadingNvidia, setLoadingNvidia] = useState(
    cachedBuildableVersions === null,
  );
  const [nvidiaUnavailable, setNvidiaUnavailable] = useState(false);
  const [buildStatus, setBuildStatus] =
    useState<BuildStatus>(IDLE_BUILD_STATUS);
  const [checkResult, setCheckResult] = useState<CheckResult | null>(null);
  const [checkingBuildable, setCheckingBuildable] = useState(false);
  const [view, setView] = useState<"main" | "settings">("main");
  const { info: pluginUpdateInfo } = usePluginUpdate();
  const [canRollback, setCanRollback] = useState(false);
  // Every source/channel is visible by default. Arch packages belong to
  // Recommended; NVIDIA-only releases use their NVIDIA category.
  const [channelFilters, setChannelFiltersState] = useState<ChannelFilters>(
    () =>
      cachedChannelFilters ?? {
        recommended: true,
        nfb: true,
        beta: true,
        nvidia_other: true,
      },
  );
  const setChannelFilters = (
    update: ChannelFilters | ((f: ChannelFilters) => ChannelFilters),
  ) => {
    setChannelFiltersState((prev) => {
      const next = typeof update === "function" ? update(prev) : update;
      cachedChannelFilters = next;
      return next;
    });
  };

  const refreshStatus = async () => {
    const epoch = statusEpoch.current;
    try {
      const data = await withTimeout(call<[], UpdateStatus>("get_status"));
      if (epoch === statusEpoch.current) {
        setStatus(data);
        setStatusLoaded(true);
      }
    } catch (error) {
      if (epoch === statusEpoch.current) setStatusLoaded(false);
      throw error;
    }
  };

  useEffect(() => {
    let closed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const epoch = statusEpoch.current;
      try {
        const data = await withTimeout(call<[], UpdateStatus>("get_status"));
        if (!closed && epoch === statusEpoch.current) {
          setStatus(data);
          setStatusLoaded(true);
        }
      } catch {
        if (!closed && epoch === statusEpoch.current) setStatusLoaded(false);
      }
      if (!closed) timer = setTimeout(poll, 5000);
    };
    void poll();
    return () => {
      closed = true;
      clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    call<[], string>("get_current_version").then(setCurrentVersion);
    call<[], RepoInfo>("get_driver_repo_info").then(setRepoInfo);
    call<[], string[]>("list_driver_versions_detailed").then((v) => {
      console.log(`[nvidia-update] Arch detailed versions (${v.length}):`, v);
      setDetailedVersions(v);
    });
    call<[], NvidiaVersionMap>("get_nvidia_branch_info").then((m) => {
      console.log(
        `[nvidia-update] NVIDIA classified versions (${Object.keys(m).length}):`,
        m,
      );
      cachedNvidiaVersions = m;
      setNvidiaVersions(m);
    });
    call<[], string[]>("list_driver_versions")
      .then((v) => {
        console.log(
          `[nvidia-update] Arch collapsed versions (${v.length}):`,
          v,
        );
        setVersions(v);
      })
      .finally(() => setLoadingVersions(false));
    call<[], Settings>("get_settings").then((s) => {
      setShowBuildNumbers(s.show_build_numbers);
      setDefaultChannel(s.default_channel);
      setShowChannelFilters(s.channel_filters_expanded);
    });
    call<[], { status: string }>("get_transaction_status").then((s) =>
      setCanRollback(s.status === "ready"),
    );
    refreshCustomBuilds();
    call<[], string[]>("list_buildable_versions")
      .then((v) => {
        if (v.length) {
          cachedBuildableVersions = v;
          setBuildableVersions(v);
        }
        setNvidiaUnavailable(v.length === 0);
      })
      .catch(() => setNvidiaUnavailable(true))
      .finally(() => setLoadingNvidia(false));
  }, []);
  // `selected` is never loaded from persisted settings: opening the plugin
  // should default to whatever is newest right now, not silently re-offer
  // the last version you actually installed. Changing the dropdown only
  // affects this session.

  // Persist whichever toggle just changed, merged with the current values
  // of the others (React state for the others hasn't updated yet within
  // this same tick, so use the closure values directly).
  const persistSettings = (patch: Partial<Settings>) => {
    const next: Settings = {
      show_build_numbers: patch.show_build_numbers ?? showBuildNumbers,
      default_channel: patch.default_channel ?? defaultChannel,
      channel_filters_expanded:
        patch.channel_filters_expanded ?? showChannelFilters,
    };
    call<[Settings], boolean>("save_settings", next);
  };

  // Defensive fallback: list_driver_versions() (the collapsed list, driven
  // by the LOCAL bin/steamos-nvidia-update.sh --list) and
  // list_driver_versions_detailed() (an independent direct curl to
  // archive.archlinux.org) can fail independently of each other — this
  // really happened once when a broken release zip shipped without bin/ at
  // all, silently emptying just the collapsed list and leaving the dropdown
  // with nothing to show. Deriving a collapsed list from the detailed one
  // whenever the former comes back empty means a single missing/failing
  // script never fully blanks the picker.
  const effectiveVersions =
    versions.length > 0
      ? versions
      : Array.from(
          new Set(detailedVersions.map((v) => v.replace(/-\d+$/, ""))),
        );

  // Use the same source/channel definition for notices and the picker.
  const latestVersion = computeChannelLatest(
    defaultChannel,
    detailedVersions,
    nvidiaVersions,
    repoInfo.stable,
    buildableVersions,
    customBuilds.map((b) => b.version),
  );
  const counts = channelCounts(
    [...versions, ...detailedVersions],
    buildableVersions,
    customBuilds.map((b) => b.version),
    nvidiaVersions,
  );
  const channelFilterLabel = (label: string, count: number) => (
    <span style={{ display: "block", textAlign: "left" }}>
      <span style={{ display: "block", fontSize: "14px", lineHeight: "18px" }}>
        {label}
      </span>
      <span
        style={{
          display: "block",
          fontSize: "11px",
          lineHeight: "16px",
          fontVariantNumeric: "tabular-nums",
          opacity: 0.8,
        }}
      >
        {t("channel_driver_count", { count })}
      </span>
    </span>
  );

  // Show notes only for the selected release, never for an unrelated old one.
  const notesEntry: { version: string; date: string; notes: string[] } | null =
    (() => {
      const collapsedChosen = chosenVersion.replace(/-\d+$/, "");
      const direct = nvidiaVersions[collapsedChosen];
      if (direct) {
        return {
          version: collapsedChosen,
          date: direct.date,
          notes: direct.notes,
        };
      }
      return null;
    })();

  const onShowReleaseNotes = () => {
    if (!notesEntry) return;
    showModal(
      <ReleaseNotesModal
        version={notesEntry.version}
        date={notesEntry.date}
        notes={notesEntry.notes}
      />,
      window,
    );
  };

  useEffect(() => {
    const listener = (data: UpdateStatus) => {
      statusEpoch.current++;
      setStatus(data);
      setStatusLoaded(true);
    };
    addEventListener("nvidia_update_progress", listener);
    return () => removeEventListener("nvidia_update_progress", listener);
  }, []);

  // Fires once per run: this effect only re-runs when done/success actually
  // change value, not on every unrelated re-render (React skips it otherwise).
  useEffect(() => {
    if (!status.done) return;
    call<[], { status: string }>("get_transaction_status").then((s) =>
      setCanRollback(s.status === "ready"),
    );
    if (status.success) {
      // Preparation changes the inactive slot; the active version stays unchanged.
      call<[], string>("get_current_version").then(setCurrentVersion);
      refreshCustomBuilds();
      pluginToast({
        title: t(
          status.action === "rollback"
            ? "rollback_ready"
            : "toast_installed_title",
        ),
        body: t(
          status.pending_reboot === false
            ? "no_restart_pending"
            : status.action === "rollback"
              ? "rollback_restart_body"
              : "toast_installed_body",
        ),
      });
    } else if (status.error_code !== "launch_failed") {
      pluginToast({
        title: t("toast_failed_title"),
        body:
          status.error_code === "arch_server_unreachable"
            ? t("error_arch_server_unreachable")
            : status.error || t("toast_failed_body_default"),
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status.done, status.success]);

  // Numeric compare (pkgver-pkgrel, e.g. "610.43.03-3") — not just a
  // rebuild-only pkgrel bump (-3 → -5) still counts as "update available",
  // but also: a custom-built version numerically AHEAD of anything Arch
  // currently ships (e.g. installed 615.71.09, Arch's newest is 610.57.04)
  // counts as up to date, not "update available" — only an Arch-provided
  // version genuinely newer than what's installed should prompt one, since
  // version numbers are incremental and Arch will never "catch down".
  const hasCurrentAndLatest = currentVersion !== "" && latestVersion !== "";
  const versionCmp = hasCurrentAndLatest
    ? compareDriverVersions(currentVersion, latestVersion)
    : 0;
  const isUpToDate = hasCurrentAndLatest && versionCmp >= 0;
  const updateAvailable = hasCurrentAndLatest && versionCmp < 0;

  // Is the currently-selected dropdown entry the same build as what's
  // already installed? A detailed pick ("610.43.03-5") compares exactly; a
  // collapsed pick ("610.43.03") only has the pkgver, so it's compared
  // against the installed pkgver alone — good enough to stop an obviously
  // pointless reinstall click, since the UI doesn't expose --force anyway.
  const selectedIsInstalled =
    currentVersion !== "" &&
    chosenVersion !== "" &&
    (showBuildNumbers
      ? chosenVersion === currentVersion
      : chosenVersion === currentVersion.replace(/-\d+$/, ""));

  // A version needs building first when it isn't already sitting in the
  // custom-build cache AND Arch doesn't provide it either — i.e. it's only
  // present because buildableVersions (every NVIDIA release, see
  // list_buildable_versions) offered it. Bare-version comparisons
  // throughout since customBuilds/buildableVersions never carry a pkgrel
  // suffix, same reasoning as VersionDropdown's own currentVersion matching.
  const chosenVersionBare = chosenVersion.replace(/-\d+$/, "");
  const needsBuild =
    chosenVersion !== "" &&
    !customBuilds.some((b) => b.version === chosenVersionBare) &&
    !effectiveVersions.includes(chosenVersionBare) &&
    !detailedVersions.some((v) => v.replace(/-\d+$/, "") === chosenVersionBare);

  // Pre-flight check whenever the selection lands on a to-build version —
  // greys out Install with a clear reason instead of letting the user sit
  // through a build that's doomed to fail partway through (see
  // check_buildable's own docstring on the backend for why this can't be
  // fully free, only far cheaper than an actual build attempt).
  useEffect(() => {
    if (!needsBuild) {
      setCheckingBuildable(false);
      setCheckResult(null);
      return;
    }
    let cancelled = false;
    setCheckingBuildable(true);
    setCheckResult(null);
    call<[string], CheckResult>("check_buildable", chosenVersionBare)
      .then((r) => {
        if (!cancelled) setCheckResult(r);
      })
      .finally(() => {
        if (!cancelled) setCheckingBuildable(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [needsBuild, chosenVersionBare]);

  // Only a confirmed answer blocks Install — a failed/inconclusive check
  // (checked_ok: false, e.g. a network hiccup) means "unknown", not "known
  // bad", so the option stays available rather than being blocked on a guess.
  const knownNotBuildable =
    checkResult?.checked_ok === true && !checkResult.buildable;

  useEffect(() => {
    const listener = (data: BuildStatus) => setBuildStatus(data);
    addEventListener("nvidia_build_progress", listener);
    return () => removeEventListener("nvidia_build_progress", listener);
  }, []);

  const startUpdate = async (specOverride?: string) => {
    // chosenVersion is always already a concrete version (VersionDropdown
    // never reports "latest" as a sentinel, and resolves the testing branch
    // itself when the beta toggle wants it) — nothing special here.
    const spec = specOverride ?? chosenVersion;
    // Optimistic update: don't wait for the first streamed line from the
    // script to show "running" — otherwise there's a window (can be tens
    // of seconds) right after clicking where the UI still shows whatever
    // was left over from the last run, the button stays enabled, and a
    // second click confusingly reports "already running" with nothing
    // having visibly happened yet.
    setStatus({ ...IDLE_STATUS, running: true, message: "Starting…" });
    statusEpoch.current++;

    const result = await requestOperation(
      () =>
        call<[string, boolean, boolean], LaunchResult>(
          "request_update",
          spec,
          false,
          needsBuild ||
            (customBuilds.some(
              (b) => b.version === spec.replace(/-\d+$/, ""),
            ) &&
              !effectiveVersions.includes(spec.replace(/-\d+$/, ""))),
        ),
      refreshStatus,
    );
    if (!result.started) {
      pluginToast({
        title: t(launchErrorTitle(result.code)),
        body: result.message || t("toast_failed_body_default"),
      });
    }
  };

  const onStart = async (specOverride?: string) => {
    setRequestPending(true);
    try {
      const info = await withTimeout(
        call<[], RootfsSpaceInfo>("get_rootfs_space_info"),
      );
      if (!info.available || info.undersized) {
        pluginToast({
          title: t("failed_title"),
          body:
            info.error ||
            t("home_space_required", {
              required: (info.required_mb / 1024).toFixed(1),
              available: (info.avail_mb / 1024).toFixed(1),
            }),
        });
        return;
      }
      await startUpdate(specOverride);
    } catch {
      setStatusLoaded(false);
      pluginToast({
        title: t("operation_request_failed"),
        body: t("status_unavailable"),
      });
    } finally {
      setRequestPending(false);
    }
  };

  const onRollback = async () => {
    setRequestPending(true);
    statusEpoch.current++;
    setStatus({
      ...IDLE_STATUS,
      running: true,
      message: t("rollback_running"),
    });
    try {
      const result = await requestOperation(
        () => call<[], LaunchResult>("request_rollback"),
        refreshStatus,
      );
      if (!result.started)
        pluginToast({
          title: t(launchErrorTitle(result.code)),
          body: result.message || t("toast_failed_body_default"),
        });
    } finally {
      setRequestPending(false);
    }
  };

  const onReboot = async () => {
    await call<[], boolean>("reboot_system");
  };

  const progressPct = status.total_steps
    ? Math.round((status.step / status.total_steps) * 100)
    : 0;
  const buildProgressPct = buildStatus.total_steps
    ? Math.round((buildStatus.step / buildStatus.total_steps) * 100)
    : 0;

  const goToSettings = () => setView("settings");

  if (view === "settings") {
    return (
      <SettingsView
        onBack={() => setView("main")}
        pluginUpdateBlocked={
          !statusLoaded ||
          requestPending ||
          status.running ||
          buildStatus.running
        }
      />
    );
  }

  return (
    <>
      <PanelSection>
        {!statusLoaded && (
          <PanelSectionRow>
            <div style={{ fontSize: 12, marginBottom: 8 }}>
              {t("status_unavailable")}
            </div>
            <ButtonItem
              layout="below"
              onClick={() => {
                void refreshStatus().catch(() => {});
              }}
              {...helpProps("reconnect")}
            >
              {t("reconnect_status")}
            </ButtonItem>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 8,
              marginBottom: 8,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <SiNvidia size={20} color="#76B900" />
              <span style={{ fontSize: 16, fontWeight: 700 }}>
                NVIDIA Driver
              </span>
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <ActionButton onClick={() => showModal(<HelpIndex />)}>
                <FaInfoCircle size={14} />
              </ActionButton>
              <ActionButton onClick={goToSettings} {...helpProps("settings")}>
                <FaCog size={14} />
              </ActionButton>
            </div>
          </div>
        </PanelSectionRow>
      </PanelSection>

      <PluginUpdateBanner
        info={pluginUpdateInfo}
        onClick={() => {
          pluginUpdateFocus.markExpanded();
          goToSettings();
        }}
      />
      <WhatsNewBanner
        versions={getWhatsNewVersions()}
        onFeatureRequest={() => {
          featureRequestFocus.markExpanded();
          goToSettings();
        }}
      />
      <OtherPluginsBanner
        onOpenSettings={() => {
          otherPluginsFocus.markExpanded();
          goToSettings();
        }}
      />

      <PanelSection>
        <PanelSectionRow>
          <div style={{ fontSize: 13 }}>
            {t("installed", {
              version: currentVersion || t("installed_none"),
            })}
          </div>
          {isUpToDate && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                fontSize: 12,
                color: "#5fdb6a",
                marginTop: 4,
              }}
            >
              <FaCheckCircle /> {t("up_to_date")}
            </div>
          )}
          {updateAvailable && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                fontSize: 12,
                color: "#ffcf5c",
                marginTop: 4,
              }}
            >
              <FaArrowCircleUp />{" "}
              {t("update_available", { version: latestVersion })}
            </div>
          )}
          {!hasCurrentAndLatest &&
            !loadingVersions &&
            currentVersion === "" && (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  fontSize: 12,
                  opacity: 0.7,
                  marginTop: 4,
                }}
              >
                <FaInfoCircle />{" "}
                {latestVersion
                  ? t("no_driver_with_latest", { version: latestVersion })
                  : t("no_driver")}
              </div>
            )}
        </PanelSectionRow>

        <PanelSectionRow>
          <div style={{ fontSize: 14, marginBottom: 4 }}>
            {t("driver_version_label")}
          </div>
          {(loadingNvidia || nvidiaUnavailable) && (
            <div style={{ fontSize: 11, opacity: 0.75 }}>
              {t(
                loadingNvidia
                  ? "nvidia_catalog_loading"
                  : "nvidia_catalog_unavailable",
              )}
            </div>
          )}
        </PanelSectionRow>
        <PanelSectionRow>
          <VersionDropdown
            versions={effectiveVersions}
            detailedVersions={detailedVersions}
            showBuildNumbers={showBuildNumbers}
            repoInfo={repoInfo}
            latestVersion={latestVersion}
            nvidiaVersions={nvidiaVersions}
            channelFilters={channelFilters}
            defaultChannel={defaultChannel}
            categoryLabels={{
              recommended: t("tag_recommended"),
              nfb: t("tag_nfb"),
              beta: t("tag_beta"),
              nvidia_other: t("tag_other"),
            }}
            archTestingLabel={t("arch_testing_label")}
            customBuildVersions={customBuilds.map((b) => b.version)}
            customBuildLabel={t("custom_build_tag")}
            buildableVersions={buildableVersions}
            needsBuildLabel={t("custom_build_needs_build_tag")}
            currentVersion={currentVersion}
            disabled={status.running || buildStatus.running}
            onSelectionChange={setChosenVersion}
          />
        </PanelSectionRow>

        {needsBuild &&
          !status.running &&
          !buildStatus.running &&
          !checkingBuildable && (
            <PanelSectionRow>
              {knownNotBuildable ? (
                <div style={{ fontSize: 11, color: "#ff6b6b" }}>
                  {t("custom_build_not_buildable")}
                </div>
              ) : (
                <div style={{ fontSize: 11, color: "#ffcf5c" }}>
                  {t("custom_build_will_build_notice")}
                </div>
              )}
            </PanelSectionRow>
          )}

        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={onShowReleaseNotes}
            {...helpProps("notes")}
            disabled={!notesEntry}
          >
            {t("release_notes_button")}
          </ButtonItem>
        </PanelSectionRow>

        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => onStart()}
            {...helpProps("install")}
            disabled={
              !statusLoaded ||
              requestPending ||
              status.running ||
              buildStatus.running ||
              loadingVersions ||
              selectedIsInstalled ||
              chosenVersion === "" ||
              (needsBuild && (checkingBuildable || knownNotBuildable))
            }
          >
            {buildStatus.running ? (
              t("custom_build_running")
            ) : status.running ? (
              t("install_button_running")
            ) : selectedIsInstalled ? (
              t("install_button_installed")
            ) : checkingBuildable ? (
              t("custom_build_checking")
            ) : (
              <div style={{ whiteSpace: "pre-line" }}>
                {t("install_button", { version: chosenVersion })}
              </div>
            )}
          </ButtonItem>
        </PanelSectionRow>

        {selectedIsInstalled && !status.running && !buildStatus.running && (
          <PanelSectionRow>
            <div style={{ fontSize: 12, color: "#b8bcbf" }}>
              {t("already_installed_notice")}
            </div>
          </PanelSectionRow>
        )}

        {(status.running || buildStatus.running) && (
          <PanelSectionRow>
            <SeparatedBlock bottomBorder={false}>
              <div
                style={{
                  fontSize: 12,
                  marginBottom: 6,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {buildStatus.running
                  ? `${t("step_label", { step: buildStatus.step, total: buildStatus.total_steps })}${buildStatus.message ? ` — ${buildStatus.message}` : ""}`
                  : `${t("step_label", { step: status.step, total: status.total_steps })}${
                      status.message ? ` — ${status.message}` : ""
                    }`}
              </div>
              <div
                style={{
                  width: "100%",
                  height: 4,
                  background: "rgba(255,255,255,0.15)",
                  borderRadius: 2,
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    width: buildStatus.running
                      ? buildStatus.step === 0
                        ? "100%"
                        : `${buildProgressPct}%`
                      : status.step === 0
                        ? "100%"
                        : `${progressPct}%`,
                    height: "100%",
                    background: "#66c0f4",
                    opacity:
                      (buildStatus.running ? buildStatus.step : status.step) ===
                      0
                        ? 0.4
                        : 1,
                    transition: "width 0.3s ease",
                  }}
                />
              </div>
            </SeparatedBlock>
          </PanelSectionRow>
        )}

        {status.done && status.success && (
          <PanelSectionRow>
            <SeparatedBlock>
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  textAlign: "center",
                  padding: "18px 10px 14px",
                  borderRadius: 8,
                  background: "rgba(95, 219, 106, 0.12)",
                  border: "1px solid rgba(95, 219, 106, 0.35)",
                }}
              >
                <FaCheckCircle
                  style={{ fontSize: 42, color: "#5fdb6a", marginBottom: 8 }}
                />
                <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 4 }}>
                  {t(
                    status.action === "rollback"
                      ? "rollback_ready"
                      : "done_title",
                    {
                      version: status.driver_spec || chosenVersion,
                    },
                  )}
                </div>
                <div style={{ fontSize: 11, opacity: 0.75, marginBottom: 14 }}>
                  {t(
                    status.pending_reboot === false
                      ? "no_restart_pending"
                      : status.action === "rollback"
                        ? "rollback_restart_body"
                        : "done_body",
                  )}
                </div>
                {status.pending_reboot !== false && (
                  <ButtonItem
                    layout="below"
                    bottomSeparator="none"
                    highlightOnFocus={false}
                    onClick={onReboot}
                    {...helpProps("reboot")}
                  >
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        gap: 6,
                      }}
                    >
                      <FaPowerOff /> {t("reboot_button")}
                    </div>
                  </ButtonItem>
                )}
              </div>
            </SeparatedBlock>
          </PanelSectionRow>
        )}

        {status.done && status.success === false && (
          <PanelSectionRow>
            <SeparatedBlock>
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  textAlign: "center",
                  padding: "18px 10px 14px",
                  borderRadius: 8,
                  background: "rgba(255, 107, 107, 0.12)",
                  border: "1px solid rgba(255, 107, 107, 0.35)",
                }}
              >
                <FaTimesCircle
                  style={{ fontSize: 42, color: "#ff6b6b", marginBottom: 8 }}
                />
                <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 4 }}>
                  {t("failed_title")}
                </div>
                <div style={{ fontSize: 11, opacity: 0.75 }}>
                  {t("failed_body", {
                    error:
                      status.error_code === "arch_server_unreachable"
                        ? t("error_arch_server_unreachable")
                        : status.error || t("failed_default_error"),
                  })}
                </div>
              </div>
            </SeparatedBlock>
          </PanelSectionRow>
        )}

        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => showModal(<UpdateLogModal />)}
            {...helpProps("log")}
          >
            {t("view_log")}
          </ButtonItem>
        </PanelSectionRow>

        <PanelSectionRow>
          <div style={{ fontSize: 11, opacity: 0.75, marginBottom: 8 }}>
            {t("prepare_notice")}
          </div>
          <ButtonItem
            layout="below"
            disabled={
              !statusLoaded || requestPending || status.running || !canRollback
            }
            {...helpProps("rollback")}
            onClick={() =>
              showModal(
                <ConfirmModal
                  strTitle={t("rollback_button")}
                  strDescription={t("rollback_confirm")}
                  onOK={onRollback}
                />,
              )
            }
          >
            {t("rollback_button")}
          </ButtonItem>
        </PanelSectionRow>

        <PanelSectionRow>
          <ToggleField
            label={t("show_builds_label")}
            {...helpProps("builds")}
            checked={showBuildNumbers}
            onChange={(v: boolean) => {
              // No explicit reset needed: VersionDropdown's own fallback
              // logic (computed at render time from its `showBuildNumbers`
              // prop) picks up the matching-granularity latest automatically
              // once its current pick no longer exists in the new list.
              setShowBuildNumbers(v);
              persistSettings({ show_build_numbers: v });
            }}
            disabled={status.running || loadingVersions}
          />
        </PanelSectionRow>

        <PanelSectionRow>
          <AnchoredDropdown
            label={t("default_channel_label")}
            {...helpProps("channel")}
            options={[
              { value: "recommended", label: t("channel_recommended") },
              { value: "nfb", label: t("channel_nfb") },
              { value: "beta", label: t("channel_beta") },
              { value: "nvidia_other", label: t("channel_nvidia_other") },
              { value: "all", label: t("channel_all") },
              { value: "tocompile", label: t("channel_tocompile") },
            ]}
            selectedValue={defaultChannel}
            onChange={(value) => {
              const v = value as DefaultChannelChoice;
              setDefaultChannel(v);
              persistSettings({ default_channel: v });
            }}
          />
        </PanelSectionRow>

        <PanelSectionRow>
          <CollapsibleSection
            label={t("channel_filter_label")}
            helpTopic="filters"
            expanded={showChannelFilters}
            onToggle={() => {
              const next = !showChannelFilters;
              setShowChannelFilters(next);
              persistSettings({ channel_filters_expanded: next });
            }}
          >
            <PanelSectionRow>
              <ToggleField
                label={channelFilterLabel(
                  t("channel_recommended"),
                  counts.recommended,
                )}
                checked={channelFilters.recommended}
                {...helpProps("recommended")}
                onChange={(v: boolean) =>
                  setChannelFilters((f) => ({ ...f, recommended: v }))
                }
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField
                label={channelFilterLabel(t("channel_nfb"), counts.nfb)}
                checked={channelFilters.nfb}
                {...helpProps("nfb")}
                onChange={(v: boolean) =>
                  setChannelFilters((f) => ({ ...f, nfb: v }))
                }
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField
                label={channelFilterLabel(t("channel_beta"), counts.beta)}
                checked={channelFilters.beta}
                {...helpProps("beta")}
                onChange={(v: boolean) =>
                  setChannelFilters((f) => ({ ...f, beta: v }))
                }
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField
                label={channelFilterLabel(
                  t("channel_nvidia_other"),
                  counts.nvidia_other,
                )}
                checked={channelFilters.nvidia_other}
                {...helpProps("other")}
                onChange={(v: boolean) =>
                  setChannelFilters((f) => ({ ...f, nvidia_other: v }))
                }
              />
            </PanelSectionRow>
          </CollapsibleSection>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  loadTranslations();

  // Fired by Plugin._main() on the Python side (see decky-plugin-toolkit's
  // PluginUpdaterMixin) as soon as Decky loads this plugin — not gated
  // behind the user ever opening its panel, unlike PluginUpdateProvider's
  // own on-mount check.
  const updateListener = addEventListener(
    "plugin_update_available",
    (info: PluginUpdateInfo) => {
      pluginToast({
        title: i18n.t("settings:settings_title"),
        body: i18n.t("settings:update_available", {
          version: info?.latest_version,
        }),
      });
    },
  );

  // Runs once when Decky (re)loads this plugin's frontend bundle — which
  // happens at Decky startup, and in practice also whenever the Steam
  // Client's own UI reloads (e.g. switching Desktop Mode <-> Gamescope
  // restarts the Steam UI process this is injected into). Not something
  // I can verify from here without a real device, but it should cover
  // both triggers you mentioned. Scoped to the user's chosen default
  // channel (same as the in-panel badge, via computeChannelLatest) — only
  // the "all" default falls back to plain Arch-stable, so this doesn't nudge
  // someone who's never touched the channel setting onto a testing build.
  (async () => {
    try {
      const [current, repo, detailed, nvidiaVersions, settings, buildable] =
        await Promise.all([
          call<[], string>("get_current_version"),
          call<[], RepoInfo>("get_driver_repo_info"),
          call<[], string[]>("list_driver_versions_detailed"),
          call<[], NvidiaVersionMap>("get_nvidia_branch_info"),
          call<[], Settings>("get_settings"),
          call<[], string[]>("list_buildable_versions"),
        ]);
      const latest = computeChannelLatest(
        settings.default_channel,
        detailed,
        nvidiaVersions,
        repo.stable,
        buildable,
      );
      if (current && latest && compareDriverVersions(current, latest) < 0) {
        pluginToast({
          title: i18n.t("toast_update_available_title"),
          body: `${current} → ${latest}`,
        });
      }
    } catch {
      // best-effort notification only
    }
  })();

  return {
    name: "decky-nvidia-update",
    titleView: <div className={staticClasses.Title}>{i18n.t("title")}</div>,
    content: (
      <PluginUpdateProvider>
        <OtherPluginsProvider selfPluginId={SELF_PLUGIN_ID}>
          <WhatsNewProvider currentVersion={CURRENT_VERSION}>
            <Content />
          </WhatsNewProvider>
        </OtherPluginsProvider>
      </PluginUpdateProvider>
    ),
    icon: <SiNvidia color="#76B900" />,
    onDismount() {
      removeEventListener("plugin_update_available", updateListener);
    },
  };
});
