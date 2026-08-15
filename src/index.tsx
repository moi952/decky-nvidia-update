import { useEffect, useState } from "react";
import {
  definePlugin,
  call,
  addEventListener,
  removeEventListener,
  toaster,
} from "@decky/api";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  ToggleField,
  DropdownItem,
  showModal,
  staticClasses,
} from "@decky/ui";
import { useTranslation } from "react-i18next";
import i18n from "i18next";
import { loadTranslations } from "./i18n";
import { VersionDropdown } from "./VersionDropdown";
import type {
  NvidiaVersionMap,
  ChannelFilters,
  DefaultChannelChoice,
} from "./VersionDropdown";
import { ReleaseNotesModal } from "./ReleaseNotesModal";
import { SeparatedBlock } from "./SeparatedBlock";
import { CollapsibleSection } from "./CollapsibleSection";
import { PluginUpdateBanner, PluginUpdateSection } from "./PluginUpdate";
import type { PluginUpdateInfo } from "./PluginUpdate";
import { SiNvidia } from "react-icons/si";
import {
  FaCheckCircle,
  FaTimesCircle,
  FaArrowCircleUp,
  FaInfoCircle,
  FaPowerOff,
} from "react-icons/fa";

type UpdateStatus = {
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
};

// The bash script's own step() text is English-only (plain bash has no
// i18n) — status.step is a reliable, language-independent index (1-8)
// instead, mapped here to a translated description for display. The raw
// English status.message is only ever used as a defensive fallback if step
// is somehow out of this range.
const STEP_DESCRIPTION_KEYS = [
  "step_1_desc",
  "step_2_desc",
  "step_3_desc",
  "step_4_desc",
  "step_5_desc",
  "step_6_desc",
  "step_7_desc",
  "step_8_desc",
];

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

const IDLE_REPO_INFO: RepoInfo = {
  stable: "",
  with_testing: "",
  testing_versions: [],
};

const IDLE_NVIDIA_VERSIONS: NvidiaVersionMap = {};

// "Latest" within a given channel — mirrors VersionDropdown's own
// preferredOption logic (walk the newest-first Arch list, take the first
// version NVIDIA classifies into that category), but always over the
// pkgver-pkgrel detailed list so the result stays comparable to
// currentVersion/repoInfo.stable (a rebuild-only pkgrel bump must still
// count as an update). "all", or a channel with nothing currently
// classified into it, falls back to the plain Arch-stable pick.
function computeChannelLatest(
  channel: DefaultChannelChoice,
  detailedVersions: string[],
  nvidiaVersions: NvidiaVersionMap,
  archStable: string,
): string {
  if (channel === "all") return archStable;
  const match = detailedVersions.find(
    (v) => nvidiaVersions[v.replace(/-\d+$/, "")]?.category === channel,
  );
  return match ?? archStable;
}

// Decky/Steam remounts this whole panel on certain interactions (confirmed
// earlier via mount-id logging — picking a dropdown option is one trigger).
// Normal useState resets on that remount; these two module-level caches
// survive it (same trick as VersionDropdown's own lastSelected), so a
// remount mid-session doesn't momentarily null out the fetched NVIDIA
// version data or silently revert the user's channel-filter picks back to
// their defaults — both of which produced visibly wrong/confusing behavior
// (a version losing its "(NFB)" label, or a filter re-checking itself).
let cachedNvidiaVersions: NvidiaVersionMap | null = null;
let cachedChannelFilters: ChannelFilters | null = null;

function Content() {
  const { t } = useTranslation();
  const [currentVersion, setCurrentVersion] = useState<string>("");
  const [repoInfo, setRepoInfo] = useState<RepoInfo>(IDLE_REPO_INFO);
  const [defaultChannel, setDefaultChannel] = useState<DefaultChannelChoice>("all");
  const [logPath, setLogPath] = useState<string>("");
  const [versions, setVersions] = useState<string[]>([]);
  const [detailedVersions, setDetailedVersions] = useState<string[]>([]);
  const [showBuildNumbers, setShowBuildNumbers] = useState<boolean>(false);
  // Set by VersionDropdown's onSelectionChange — that component owns the
  // actual selection state/UI entirely; this is just the last value it
  // reported up, used for the install button and selectedIsInstalled below.
  const [chosenVersion, setChosenVersion] = useState<string>("");
  const [loadingVersions, setLoadingVersions] = useState<boolean>(true);
  const [status, setStatus] = useState<UpdateStatus>(IDLE_STATUS);
  const [showLog, setShowLog] = useState<boolean>(false);
  const [nvidiaVersions, setNvidiaVersions] = useState<NvidiaVersionMap>(
    () => cachedNvidiaVersions ?? IDLE_NVIDIA_VERSIONS,
  );
  const [showChannelFilters, setShowChannelFilters] = useState<boolean>(true);
  const [pluginUpdateInfo, setPluginUpdateInfo] =
    useState<PluginUpdateInfo | null>(null);
  const [checkingPluginUpdate, setCheckingPluginUpdate] =
    useState<boolean>(false);
  const [pluginUpdateExpanded, setPluginUpdateExpanded] =
    useState<boolean>(false);
  // All checked by default: every classified version is shown; unchecking a
  // category hides just the versions classified into it (an unclassified
  // Arch version is never hidden, whatever the filter state).
  const [channelFilters, setChannelFiltersState] = useState<ChannelFilters>(
    () => cachedChannelFilters ?? { recommended: true, nfb: true, beta: true },
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

  useEffect(() => {
    call<[], string>("get_current_version").then(setCurrentVersion);
    call<[], RepoInfo>("get_driver_repo_info").then(setRepoInfo);
    call<[], UpdateStatus>("get_status").then(setStatus);
    call<[], string>("get_log_path").then(setLogPath);
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
        console.log(`[nvidia-update] Arch collapsed versions (${v.length}):`, v);
        setVersions(v);
      })
      .finally(() => setLoadingVersions(false));
    call<[], Settings>("get_settings").then((s) => {
      setShowBuildNumbers(s.show_build_numbers);
      setDefaultChannel(s.default_channel);
      setShowChannelFilters(s.channel_filters_expanded);
    });
    call<[boolean], PluginUpdateInfo>("get_plugin_update_info", false).then(
      setPluginUpdateInfo,
    );
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
      : Array.from(new Set(detailedVersions.map((v) => v.replace(/-\d+$/, ""))));

  // "Latest" for the up-to-date/update-available check and badge, scoped to
  // the channel the user has chosen as default (recommended/NFB/beta) — not
  // just the plain Arch-stable pick, otherwise picking e.g. NFB as the
  // default channel still nagged about a newer Recommended/NFB release the
  // user deliberately isn't tracking. "all" keeps the old plain-stable
  // behavior.
  const latestVersion = computeChannelLatest(
    defaultChannel,
    detailedVersions,
    nvidiaVersions,
    repoInfo.stable,
  );

  // Adaptive: notes for whatever is currently selected if NVIDIA classifies
  // it (ignoring a trailing -pkgrel so a detailed pick still matches
  // NVIDIA's own bare version), otherwise fall back to the most recent
  // Recommended release we know about.
  const notesEntry: { version: string; date: string; notes: string[] } | null =
    (() => {
      const collapsedChosen = chosenVersion.replace(/-\d+$/, "");
      const direct = nvidiaVersions[collapsedChosen];
      if (direct) {
        return { version: collapsedChosen, date: direct.date, notes: direct.notes };
      }
      const recommendedEntries = Object.entries(nvidiaVersions).filter(
        ([, info]) => info.category === "recommended",
      );
      if (recommendedEntries.length === 0) return null;
      const [bestVersion, bestInfo] = recommendedEntries.reduce((a, b) =>
        new Date(a[1].date).getTime() >= new Date(b[1].date).getTime() ? a : b,
      );
      return { version: bestVersion, date: bestInfo.date, notes: bestInfo.notes };
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
    const listener = (data: UpdateStatus) => setStatus(data);
    addEventListener("nvidia_update_progress", listener);
    return () => removeEventListener("nvidia_update_progress", listener);
  }, []);

  // Fires once per run: this effect only re-runs when done/success actually
  // change value, not on every unrelated re-render (React skips it otherwise).
  useEffect(() => {
    if (!status.done) return;
    if (status.success) {
      // The install itself is already done at this point (pacman shows the
      // new package) even though a reboot is still needed to load it — the
      // "Installed: X" line and the install button's disabled state were
      // otherwise stuck showing the OLD version forever, since currentVersion
      // was only ever fetched once on mount. Re-fetching here makes both
      // update immediately: "up to date" instead of "update available", and
      // the button correctly greys out for the version just installed.
      call<[], string>("get_current_version").then(setCurrentVersion);
      toaster.toast({
        title: t("toast_installed_title"),
        body: t("toast_installed_body"),
      });
    } else {
      toaster.toast({
        title: t("toast_failed_title"),
        body:
          status.error_code === "arch_server_unreachable"
            ? t("error_arch_server_unreachable")
            : status.error || t("toast_failed_body_default"),
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status.done, status.success]);

  // Exact string compare (pkgver-pkgrel, e.g. "610.43.03-3") — not just the
  // pkgver, so a rebuild-only bump (-3 → -5) still counts as "update available".
  const hasCurrentAndLatest = currentVersion !== "" && latestVersion !== "";
  const isUpToDate = hasCurrentAndLatest && currentVersion === latestVersion;
  const updateAvailable =
    hasCurrentAndLatest && currentVersion !== latestVersion;

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

  const onStart = async () => {
    // chosenVersion is always already a concrete version (VersionDropdown
    // never reports "latest" as a sentinel, and resolves the testing branch
    // itself when the beta toggle wants it) — nothing special here.
    const spec = chosenVersion;
    // Optimistic update: don't wait for the first streamed line from the
    // script to show "running" — otherwise there's a window (can be tens
    // of seconds) right after clicking where the UI still shows whatever
    // was left over from the last run, the button stays enabled, and a
    // second click confusingly reports "already running" with nothing
    // having visibly happened yet.
    setStatus({ ...IDLE_STATUS, running: true, message: "Starting…" });

    const ok = await call<[string, boolean], boolean>(
      "start_update",
      spec,
      false,
    );
    if (!ok) {
      toaster.toast({
        title: t("toast_already_running_title"),
        body: t("toast_already_running_body"),
      });
      // Our optimistic guess was wrong (something was already running) —
      // resync with whatever real progress the backend actually has.
      call<[], UpdateStatus>("get_status").then(setStatus);
    }
  };

  const onReboot = async () => {
    await call<[], boolean>("reboot_system");
  };

  const onCheckPluginUpdate = async () => {
    setCheckingPluginUpdate(true);
    try {
      const info = await call<[boolean], PluginUpdateInfo>(
        "get_plugin_update_info",
        true,
      );
      setPluginUpdateInfo(info);
    } finally {
      setCheckingPluginUpdate(false);
    }
  };

  const progressPct = status.total_steps
    ? Math.round((status.step / status.total_steps) * 100)
    : 0;

  return (
    <PanelSection>
      <PanelSectionRow>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            marginBottom: 8,
          }}
        >
          <SiNvidia size={20} color="#76B900" />
          <span style={{ fontSize: 16, fontWeight: 700 }}>NVIDIA Driver</span>
        </div>
      </PanelSectionRow>
      <PluginUpdateBanner info={pluginUpdateInfo} />
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
        {!hasCurrentAndLatest && !loadingVersions && currentVersion === "" && (
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
          }}
          archTestingLabel={t("arch_testing_label")}
          disabled={status.running}
          onSelectionChange={setChosenVersion}
        />
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem layout="below" onClick={onShowReleaseNotes} disabled={!notesEntry}>
          {t("release_notes_button")}
        </ButtonItem>
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={onStart}
          disabled={status.running || loadingVersions || selectedIsInstalled}
        >
          {status.running ? (
            t("install_button_running")
          ) : selectedIsInstalled ? (
            t("install_button_installed")
          ) : (
            <div style={{ whiteSpace: "pre-line" }}>
              {t("install_button", { version: chosenVersion })}
            </div>
          )}
        </ButtonItem>
      </PanelSectionRow>

      {status.running && (
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
              {t("step_label", { step: status.step, total: status.total_steps })}
              {status.step >= 1 && status.step <= STEP_DESCRIPTION_KEYS.length
                ? ` — ${t(STEP_DESCRIPTION_KEYS[status.step - 1], { version: status.driver_spec })}`
                : status.message
                  ? ` — ${status.message}`
                  : ""}
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
                  width: status.step === 0 ? "100%" : `${progressPct}%`,
                  height: "100%",
                  background: "#66c0f4",
                  opacity: status.step === 0 ? 0.4 : 1,
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
                {t("done_title", { version: currentVersion || chosenVersion })}
              </div>
              <div style={{ fontSize: 11, opacity: 0.75, marginBottom: 14 }}>
                {t("done_body")}
              </div>
              <ButtonItem
                layout="below"
                bottomSeparator="none"
                highlightOnFocus={false}
                onClick={onReboot}
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

      {status.log.length > 0 && (
        <PanelSectionRow>
          <CollapsibleSection
            label={t("log_label", { count: status.log.length })}
            expanded={showLog}
            onToggle={() => setShowLog((v) => !v)}
          >
            <div
              style={{
                maxHeight: 160,
                overflowY: "auto",
                fontSize: 10,
                fontFamily: "monospace",
                whiteSpace: "pre-wrap",
                opacity: 0.8,
                background: "rgba(0,0,0,0.2)",
                padding: 6,
                borderRadius: 4,
                marginTop: 4,
              }}
            >
              {status.log.slice(-20).join("\n")}
            </div>
            {logPath && (
              <SeparatedBlock>
                <div style={{ fontSize: 10, opacity: 0.5 }}>
                  {t("full_log_label", { path: logPath })}
                </div>
              </SeparatedBlock>
            )}
          </CollapsibleSection>
        </PanelSectionRow>
      )}

      <PanelSectionRow>
        <ToggleField
          label={t("show_builds_label")}
          description={t("show_builds_desc")}
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
        <div style={{ fontSize: 14, marginTop: 12, marginBottom: 4 }}>
          {t("default_channel_label")}
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem
          rgOptions={[
            { data: "recommended", label: t("channel_recommended") },
            { data: "nfb", label: t("channel_nfb") },
            { data: "beta", label: t("channel_beta") },
            { data: "all", label: t("channel_all") },
          ]}
          selectedOption={defaultChannel}
          {...({ childrenContainerWidth: "max" } as any)}
          onChange={(o: any) => {
            const value = o?.data as DefaultChannelChoice;
            setDefaultChannel(value);
            persistSettings({ default_channel: value });
          }}
        />
      </PanelSectionRow>

      <PanelSectionRow>
        <CollapsibleSection
          label={t("channel_filter_label")}
          expanded={showChannelFilters}
          onToggle={() => {
            const next = !showChannelFilters;
            setShowChannelFilters(next);
            persistSettings({ channel_filters_expanded: next });
          }}
        >
          <PanelSectionRow>
            <ToggleField
              label={t("channel_recommended")}
              checked={channelFilters.recommended}
              onChange={(v: boolean) =>
                setChannelFilters((f) => ({ ...f, recommended: v }))
              }
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField
              label={t("channel_nfb")}
              checked={channelFilters.nfb}
              onChange={(v: boolean) =>
                setChannelFilters((f) => ({ ...f, nfb: v }))
              }
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField
              label={t("channel_beta")}
              checked={channelFilters.beta}
              onChange={(v: boolean) =>
                setChannelFilters((f) => ({ ...f, beta: v }))
              }
            />
          </PanelSectionRow>
        </CollapsibleSection>
      </PanelSectionRow>

      <PluginUpdateSection
        info={pluginUpdateInfo}
        checking={checkingPluginUpdate}
        expanded={pluginUpdateExpanded}
        onToggle={() => setPluginUpdateExpanded((v) => !v)}
        onCheckNow={onCheckPluginUpdate}
      />
    </PanelSection>
  );
}

export default definePlugin(() => {
  loadTranslations();

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
      const [current, repo, detailed, nvidiaVersions, settings] =
        await Promise.all([
          call<[], string>("get_current_version"),
          call<[], RepoInfo>("get_driver_repo_info"),
          call<[], string[]>("list_driver_versions_detailed"),
          call<[], NvidiaVersionMap>("get_nvidia_branch_info"),
          call<[], Settings>("get_settings"),
        ]);
      const latest = computeChannelLatest(
        settings.default_channel,
        detailed,
        nvidiaVersions,
        repo.stable,
      );
      if (current && latest && current !== latest) {
        toaster.toast({
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
    content: <Content />,
    icon: <SiNvidia color="#76B900" />,
  };
});
