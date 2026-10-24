import { ConfirmModal, Focusable, showModal } from "@decky/ui";
import i18n from "i18next";

export const HELP = {
  driver: {
    title: "Driver version",
    text: "Choose the NVIDIA driver to prepare for the other SteamOS system slot. The current driver stays active until you restart. Labels show whether a version comes from Arch packages or a retained custom build. 'Needs build' means the plugin must build packages locally first.\n\nA version already installed cannot be reinstalled. Changing this selection alone does not install anything.",
  },
  recommended: {
    title: "Recommended (Arch)",
    text: "These versions are available as Arch Linux packages. 'Recommended' identifies the plugin's packaged source category: it avoids building custom packages from NVIDIA's installer. The NVIDIA kernel module still needs to be built for your SteamOS kernel.\n\nIt does not mean every listed version is the newest or the best choice for your GPU. The list includes older archived releases. 'Arch testing' identifies packages from an Arch testing repository; it is separate from NVIDIA's Beta category.",
  },
  nfb: {
    title: "New Feature Branch (NFB)",
    text: "NVIDIA releases classified as New Feature Branch, focused on newer features. In this plugin, this filter contains NVIDIA-source versions that are not already listed under Arch. They may need a local build.\n\nA count of zero means the current available build catalogue has no separate entries in this category. It does not mean that all NVIDIA NFB releases have disappeared.",
  },
  beta: {
    title: "Beta",
    text: "Pre-release drivers identified as Beta by NVIDIA. They may include new features or fixes and may also have regressions. This filter contains NVIDIA-source versions outside the Arch list.\n\nThese drivers may need a local build. 'Arch testing' is an Arch repository label and is not the same as NVIDIA Beta.",
  },
  other: {
    title: "Other NVIDIA",
    text: "NVIDIA-source versions outside the Arch list that are not classified here as NFB or Beta. This can include production releases or versions with incomplete channel metadata.\n\n'Other' describes the source classification, not driver quality or compatibility. These versions may need a local build.",
  },
  filters: {
    title: "Filter by source / channel",
    text: "These switches control which categories appear in the driver picker. They do not install, remove or change the active driver.\n\nCounts describe available versions in each source category, with duplicate Arch versions counted under Arch. Availability also depends on the current package and custom-build catalogues. Use the Help action on an individual filter for its explanation.",
  },
  channel: {
    title: "Update channel",
    text: "Choose the category used for the default driver selection, update notices and the 'Up to date' comparison. All channels compares the available categories. Needs build selects versions requiring local package creation.\n\nThis setting does not install updates automatically. The source filters separately control what you can see in the picker.",
  },
  builds: {
    title: "Show exact builds (-N)",
    text: "Show individual Arch package revisions, such as 615.78.08-1 and 615.78.08-2, instead of combining them under one NVIDIA version. The number after the dash is the package revision, not a new NVIDIA driver version.\n\nThis changes the list display and lets you select an exact package revision; it does not change your installed driver.",
  },
  install: {
    title: "Install driver",
    text: "The plugin prepares and validates a compressed system image on /home, then writes it to the inactive SteamOS A/B slot. That slot's previous contents are replaced. Your currently running system is retained for rollback. Restart after preparation completes to use the new driver.\n\nThe plugin supports compatible A/B layouts with the original root size as well as previously enlarged 8 GB roots. It uses the detected target size and checks free space; it does not resize partitions. Custom versions also need time and space for a local package build.",
  },
  rollback: {
    title: "Return to previous system",
    text: "Select the retained previous system for the next boot, after verifying that both saved system identities still match. Restart to switch to it. This switches the saved system slot, not just one driver package.\n\nRollback may be unavailable if an OS update or another operation has replaced the saved system. A later driver preparation overwrites the inactive slot.",
  },
  reboot: {
    title: "Reboot now",
    text: "Restart the PC into the system selected by the completed driver preparation or rollback. Until you restart, the current driver remains loaded. Save any open work before restarting.",
  },
  log: {
    title: "Driver update log",
    text: "Open the saved operation log in a larger window. Live mode always follows the newest line at the bottom. Press the left stick (L3) to browse history with the right stick; press L3 again to return to Live. In history mode you can also use the mouse wheel or keyboard. Click the log to switch modes when using a mouse.\n\nThe log remains available after restarting Decky. Very large logs show their recent portion and the full file path. Opening or closing the viewer does not start, cancel or change the operation.",
  },
  notes: {
    title: "Release notes",
    text: "Read NVIDIA's published changes for the selected driver. Notes may not be available for every archived package. Opening this window does not install the driver. Plugin release history is a separate section in Settings.",
  },
  reconnect: {
    title: "Reconnect",
    text: "Refresh the status of the driver operation after a connection problem. A running operation belongs to systemd and can continue while Decky reloads. Reconnecting observes that existing operation; it does not start a second installation.",
  },
  settings: {
    title: "Plugin settings",
    text: "View plugin release history, check plugin releases and access project links. Plugin updates replace the Decky plugin itself; NVIDIA driver updates are a separate action on the main panel.\n\nPlugin installation is blocked while a driver operation is running or its status is unknown.",
  },
  history: {
    title: "Plugin release history",
    text: "Read changes made to the Decky plugin. This is separate from NVIDIA driver release notes and does not change the installed driver.",
  },
  plugin: {
    title: "Plugin updates and project links",
    text: "Choose a published plugin release and let Decky install its ZIP, or open the project's feedback and issue links. This updates the plugin, not the NVIDIA driver. The configured repository determines which releases are offered.",
  },
  support: {
    title: "Support",
    text: "Open the plugin author's support links. This section does not change your driver or system configuration.",
  },
} as const;

export type HelpTopic = keyof typeof HELP;

export function showHelp(topic: HelpTopic) {
  const entry = HELP[topic];
  showModal(
    <ConfirmModal
      strTitle={entry.title}
      strDescription={entry.text}
      strOKButtonText={i18n.t("panel:close_button")}
      bAlertDialog
    />,
  );
}

export function helpProps(topic: HelpTopic) {
  return {
    onSecondaryButton: (event?: {
      stopPropagation(): void;
      preventDefault(): void;
    }) => {
      event?.preventDefault();
      event?.stopPropagation();
      showHelp(topic);
    },
    onSecondaryActionDescription: "Help",
  };
}

export function HelpScope({
  topic,
  children,
}: {
  topic: HelpTopic;
  children: React.ReactNode;
}) {
  return (
    <Focusable
      {...helpProps(topic)}
      onContextMenu={(event) => {
        event.preventDefault();
        event.stopPropagation();
        showHelp(topic);
      }}
    >
      {children}
    </Focusable>
  );
}

export function HelpIndex({ closeModal }: { closeModal?: () => void }) {
  return (
    <ConfirmModal
      strTitle="Help"
      strOKButtonText={i18n.t("panel:close_button")}
      onOK={closeModal}
      onCancel={closeModal}
      bAlertDialog
    >
      <p>
        Use Help in Steam's bottom bar for the focused control, or choose a
        topic below.
      </p>
      <div style={{ maxHeight: "50vh", overflowY: "auto" }}>
        {Object.entries(HELP).map(([topic, entry]) => (
          <Focusable
            key={topic}
            onActivate={() => showHelp(topic as HelpTopic)}
            onClick={() => showHelp(topic as HelpTopic)}
            onFocus={(event) =>
              event.currentTarget.scrollIntoView({ block: "nearest" })
            }
            style={{
              padding: "10px 12px",
              marginBottom: 4,
              background: "rgba(255,255,255,0.06)",
              borderRadius: 4,
            }}
          >
            {entry.title}
          </Focusable>
        ))}
      </div>
    </ConfirmModal>
  );
}
