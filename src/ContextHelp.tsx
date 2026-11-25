import { ConfirmModal, Focusable, showModal } from "@decky/ui";
import i18n from "i18next";

// Titles and texts live in the "help" i18n namespace (help:<topic>.title/.text).
export const HELP_TOPICS = [
  "driver",
  "recommended",
  "nfb",
  "beta",
  "nvidia_recommended",
  "filters",
  "channel",
  "builds",
  "install",
  "rollback",
  "reboot",
  "log",
  "notes",
  "reconnect",
  "settings",
  "history",
  "plugin",
  "support",
] as const;

export type HelpTopic = (typeof HELP_TOPICS)[number];

export function showHelp(topic: HelpTopic) {
  showModal(
    <ConfirmModal
      strTitle={i18n.t(`help:${topic}.title`)}
      strDescription={i18n.t(`help:${topic}.text`)}
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
    onSecondaryActionDescription: i18n.t("help:ui.action"),
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
      strTitle={i18n.t("help:ui.title")}
      strOKButtonText={i18n.t("panel:close_button")}
      onOK={closeModal}
      onCancel={closeModal}
      bAlertDialog
    >
      <p>{i18n.t("help:ui.intro")}</p>
      <div style={{ maxHeight: "50vh", overflowY: "auto" }}>
        {HELP_TOPICS.map((topic) => (
          <Focusable
            key={topic}
            onActivate={() => showHelp(topic)}
            onClick={() => showHelp(topic)}
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
            {i18n.t(`help:${topic}.title`)}
          </Focusable>
        ))}
      </div>
    </ConfirmModal>
  );
}
