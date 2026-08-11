import { useEffect, useState } from "react";
import { DropdownItem } from "@decky/ui";

export type RepoInfo = {
  stable: string;
  with_testing: string;
  testing_versions: string[];
};

export type NvidiaCategory = "recommended" | "nfb" | "beta";

export type NvidiaVersionInfo = {
  category: NvidiaCategory;
  date: string;
  notes: string[];
};

// Keyed by NVIDIA's bare version string (e.g. "610.57.04"), matching
// Arch's collapsed pkgver — covers as much of NVIDIA's own history as their
// endpoint returns, NOT just one "latest" pick per category. A version
// Arch has that isn't a key here is simply unclassified.
export type NvidiaVersionMap = Record<string, NvidiaVersionInfo>;

export type ChannelFilters = {
  recommended: boolean;
  nfb: boolean;
  beta: boolean;
};

// "all" is only ever a valid value for the default-channel SETTING, never
// an actual per-version classification (a version is never tagged "all").
export type DefaultChannelChoice = NvidiaCategory | "all";

interface VersionDropdownProps {
  versions: string[];
  detailedVersions: string[];
  showBuildNumbers: boolean;
  repoInfo: RepoInfo;
  latestVersion: string;
  nvidiaVersions: NvidiaVersionMap;
  channelFilters: ChannelFilters;
  // Which category the dropdown defaults to when nothing's been picked yet
  // (user-configurable in Content, no longer hardcoded to "recommended").
  // "all" means "whichever is newest, regardless of category".
  defaultChannel: DefaultChannelChoice;
  // Localized labels — this component stays i18n-free itself (no
  // react-i18next dependency), so Content passes already-translated text.
  categoryLabels: Record<NvidiaCategory, string>;
  archTestingLabel: string;
  disabled?: boolean;
  onSelectionChange: (version: string) => void;
}

// Confirmed via instrumentation: something above this component (Decky/Steam,
// not our own render tree) tears down and recreates the ENTIRE plugin panel
// the instant a dropdown option is picked. A component's local useState
// cannot survive that. So the actual selection lives in this module-level
// variable instead — outside React entirely, untouched by the panel being
// remounted — and the component's state is just seeded from it on every
// (re)mount.
//
// That remount happens near-instantly after the click that caused it. A
// genuine "close the plugin, come back later" is a separate, much later
// mount with no such click right before it. RESTORE_WINDOW_MS tells the two
// apart: within it, trust the persisted pick (survive the spurious remount);
// past it, treat this as a fresh open and fall back to the latest version.
const RESTORE_WINDOW_MS = 5000;
let lastSelected = "";
let lastSelectedAt = 0;

export function VersionDropdown({
  versions,
  detailedVersions,
  showBuildNumbers,
  repoInfo,
  latestVersion,
  nvidiaVersions,
  channelFilters,
  defaultChannel,
  categoryLabels,
  archTestingLabel,
  disabled,
  onSelectionChange,
}: VersionDropdownProps) {
  const displayVersions = showBuildNumbers ? detailedVersions : versions;
  const testingPkgvers = repoInfo.testing_versions.map((v) =>
    v.replace(/-\d+$/, ""),
  );

  useEffect(() => {
    console.log(
      `[nvidia-update] Arch versions: ${versions.length}, detailed: ${detailedVersions.length}, ` +
        `NVIDIA classified: ${Object.keys(nvidiaVersions).length}`,
      nvidiaVersions,
    );
  }, [versions, detailedVersions, nvidiaVersions]);

  // Every Arch-available version is kept UNLESS it's classified into a
  // category whose checkbox is currently off. A version NVIDIA doesn't
  // classify at all is always shown — there's no basis to hide it.
  const options = displayVersions
    .map((v) => {
      const bareVersion = v.replace(/-\d+$/, "");
      const info = nvidiaVersions[bareVersion];
      if (info && !channelFilters[info.category]) return null;
      const isArchTesting = showBuildNumbers
        ? repoInfo.testing_versions.includes(v)
        : testingPkgvers.includes(v);
      const label = info
        ? `${v} (${categoryLabels[info.category]})`
        : isArchTesting
          ? `${v} (${archTestingLabel})`
          : v;
      return { data: v, label };
    })
    .filter((o): o is { data: string; label: string } => o !== null);

  const activeVersions = options.map((o) => o.data);

  // Prefer defaulting to whatever is tagged as the user's chosen default
  // channel in the still-visible list; otherwise fall back to the plain
  // latest (stable) pick. "all" skips the category match entirely and just
  // takes the first (newest) option, since the lists are already
  // newest-first.
  const preferredOption =
    defaultChannel === "all"
      ? options[0]
      : options.find((o) => {
          const info = nvidiaVersions[o.data.replace(/-\d+$/, "")];
          return info?.category === defaultChannel;
        });
  const fallbackDefault = showBuildNumbers
    ? latestVersion
    : latestVersion.replace(/-\d+$/, "");
  const defaultVersion = preferredOption?.data ?? fallbackDefault;

  const [selected, setSelected] = useState<string>(() =>
    Date.now() - lastSelectedAt < RESTORE_WINDOW_MS ? lastSelected : "",
  );
  const effectiveSelected =
    selected !== "" && activeVersions.includes(selected)
      ? selected
      : defaultVersion;

  // Syncs whatever is actually showing (persisted pick, or the default while
  // none has been made yet) up to the parent — fires on mount and whenever
  // it changes, so a remount immediately re-reports the persisted pick
  // instead of silently reverting the parent's chosenVersion to "".
  useEffect(() => {
    onSelectionChange(effectiveSelected);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveSelected]);

  return (
    <DropdownItem
      rgOptions={options}
      selectedOption={effectiveSelected}
      // 4.11.1's type declarations don't list this prop (only the 4.12.0
      // wrapper's types do), but the actual runtime component is Steam's
      // own internal one either way and has always accepted it — cast to
      // bypass the type-only gap.
      {...({ childrenContainerWidth: "max" } as any)}
      onChange={(o: any) => {
        const version = o?.data ?? "";
        lastSelected = version;
        lastSelectedAt = Date.now();
        setSelected(version);
        onSelectionChange(version);
      }}
      disabled={disabled}
    />
  );
}
