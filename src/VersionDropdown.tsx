import { useEffect, useState } from "react";
import { AnchoredDropdown } from "@moi952/decky-ui-kit";
import { helpProps } from "./ContextHelp";

export type RepoInfo = {
  stable: string;
  with_testing: string;
  testing_versions: string[];
};

// NVIDIA's historical metadata is used only for NVIDIA-source releases.
export type NvidiaCategory = "recommended" | "nfb" | "beta";
export type NvidiaVersionInfo = {
  category: NvidiaCategory;
  date: string;
  notes: string[];
};
export type NvidiaVersionMap = Record<string, NvidiaVersionInfo>;
export type DriverChannel = "recommended" | "nfb" | "beta" | "nvidia_other";
export type ChannelFilters = Record<DriverChannel, boolean>;
export type DefaultChannelChoice = DriverChannel | "all" | "tocompile";

export function compareDriverVersions(a: string, b: string): number {
  const aParts = a.split(/\D+/).filter(Boolean).map(Number);
  const bParts = b.split(/\D+/).filter(Boolean).map(Number);
  for (let i = 0; i < Math.max(aParts.length, bParts.length); i++) {
    const diff = (aParts[i] ?? 0) - (bParts[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

const bare = (version: string) => version.replace(/-\d+$/, "");

export function driverChannel(
  isArch: boolean,
  info?: NvidiaVersionInfo,
): DriverChannel {
  if (isArch) return "recommended";
  return info?.category === "nfb" || info?.category === "beta"
    ? info.category
    : "nvidia_other";
}

export function channelCounts(
  archVersions: string[],
  buildableVersions: string[],
  customVersions: string[],
  nvidiaVersions: NvidiaVersionMap,
): Record<DriverChannel, number> {
  const arch = new Set(archVersions.map(bare));
  const counts = { recommended: arch.size, nfb: 0, beta: 0, nvidia_other: 0 };
  for (const version of new Set([...buildableVersions, ...customVersions])) {
    if (!arch.has(version))
      counts[driverChannel(false, nvidiaVersions[version])]++;
  }
  return counts;
}

// Update notices and the picker share the same source/channel definition.
export function computeChannelLatest(
  channel: DefaultChannelChoice,
  detailedVersions: string[],
  nvidiaVersions: NvidiaVersionMap,
  archStable: string,
  buildableVersions: string[] = [],
  customVersions: string[] = [],
): string {
  const arch = new Set(detailedVersions.map(bare));
  if (archStable) arch.add(bare(archStable));
  const nvidia = Array.from(
    new Set([...buildableVersions, ...customVersions]),
  ).filter((v) => !arch.has(v));
  const versions =
    channel === "recommended"
      ? archStable
        ? [archStable]
        : detailedVersions
      : channel === "all"
        ? [...detailedVersions, ...nvidia, ...(archStable ? [archStable] : [])]
        : channel === "tocompile"
          ? nvidia.filter((v) => !customVersions.includes(v))
          : nvidia.filter(
              (v) => driverChannel(false, nvidiaVersions[v]) === channel,
            );
  return [...versions].sort((a, b) => compareDriverVersions(b, a))[0] ?? "";
}

interface VersionDropdownProps {
  versions: string[];
  detailedVersions: string[];
  showBuildNumbers: boolean;
  repoInfo: RepoInfo;
  latestVersion: string;
  nvidiaVersions: NvidiaVersionMap;
  channelFilters: ChannelFilters;
  defaultChannel: DefaultChannelChoice;
  categoryLabels: Record<DriverChannel, string>;
  archTestingLabel: string;
  customBuildVersions?: string[];
  customBuildLabel: string;
  buildableVersions?: string[];
  needsBuildLabel: string;
  currentVersion: string;
  disabled?: boolean;
  onSelectionChange: (version: string) => void;
}

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
  customBuildVersions = [],
  customBuildLabel,
  buildableVersions = [],
  needsBuildLabel,
  currentVersion,
  disabled,
  onSelectionChange,
}: VersionDropdownProps) {
  const displayVersions = showBuildNumbers ? detailedVersions : versions;
  const arch = new Set([...versions, ...detailedVersions].map(bare));
  const testing = new Set(repoInfo.testing_versions.map(bare));
  const archOptions = channelFilters.recommended
    ? displayVersions.map((v) => ({
        data: v,
        label: `${v} (${(showBuildNumbers ? repoInfo.testing_versions.includes(v) : testing.has(v)) ? archTestingLabel : "Arch"})`,
        channel: "recommended" as DriverChannel,
        needsBuild: false,
      }))
    : [];
  const nvidiaOptions = Array.from(
    new Set([...customBuildVersions, ...buildableVersions]),
  )
    .filter((v) => !arch.has(v))
    .map((v) => {
      const channel = driverChannel(false, nvidiaVersions[v]);
      const needsBuild = !customBuildVersions.includes(v);
      const tags = ["NVIDIA"];
      tags.push(categoryLabels[channel]);
      tags.push(needsBuild ? needsBuildLabel : customBuildLabel);
      return {
        data: v,
        label: `${v} (${tags.join(" · ")})`,
        channel,
        needsBuild,
      };
    })
    .filter((o) => channelFilters[o.channel]);
  const options = [...archOptions, ...nvidiaOptions].sort((a, b) =>
    compareDriverVersions(b.data, a.data),
  );
  const preferred = options.filter(
    (o) =>
      defaultChannel === "all" ||
      (defaultChannel === "tocompile"
        ? o.needsBuild
        : o.channel === defaultChannel),
  );
  const latestOption = preferred.find(
    (o) =>
      o.data === latestVersion ||
      (!showBuildNumbers && o.data === bare(latestVersion)),
  );
  const best = latestOption ?? preferred[0] ?? options[0];
  const current = preferred.find(
    (o) => o.data === currentVersion || o.data === bare(currentVersion),
  );
  const defaultVersion =
    current && best && compareDriverVersions(currentVersion, best.data) >= 0
      ? current.data
      : (best?.data ?? "");
  const [selected, setSelected] = useState<string>(() =>
    Date.now() - lastSelectedAt < RESTORE_WINDOW_MS ? lastSelected : "",
  );
  const effectiveSelected =
    selected && options.some((o) => o.data === selected)
      ? selected
      : defaultVersion;
  useEffect(() => {
    onSelectionChange(effectiveSelected);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveSelected]);

  return (
    <div style={disabled ? { opacity: 0.5, pointerEvents: "none" } : undefined}>
      <AnchoredDropdown
        {...helpProps("driver")}
        maxVisibleOptions={8}
        renderOptionLabel={(option) => (
          <span style={{ display: "block", textAlign: "left" }}>
            <span
              style={{
                display: "block",
                lineHeight: "18px",
                fontVariantNumeric: "tabular-nums",
              }}
            >
              {option.value}
            </span>
            <span
              style={{
                display: "block",
                lineHeight: "16px",
                fontSize: "11px",
                opacity: 0.8,
              }}
            >
              {option.label.slice(option.value.length + 2, -1)}
            </span>
          </span>
        )}
        options={options.map((o) => ({ value: o.data, label: o.label }))}
        selectedValue={effectiveSelected}
        onChange={(version) => {
          lastSelected = version;
          lastSelectedAt = Date.now();
          setSelected(version);
          onSelectionChange(version);
        }}
      />
    </div>
  );
}
