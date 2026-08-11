import { ConfirmModal, Focusable } from "@decky/ui";
import { useTranslation } from "react-i18next";
import i18n from "i18next";

interface ReleaseNotesModalProps {
  version: string;
  date: string;
  notes: string[];
  closeModal?: () => void;
}

// NVIDIA's ReleaseDateTime ("Mon Aug 03, 2026") is a standard English date
// string the JS Date constructor parses fine — reformatted here in the
// user's own language/locale instead of showing NVIDIA's raw English text.
function formatDate(raw: string): string {
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return raw;
  return new Intl.DateTimeFormat(i18n.language, { dateStyle: "long" }).format(d);
}

// ConfirmModal, not a bare ModalRoot with a hand-placed button: it's Decky's
// real "info dialog with a footer OK button" component (same one used by
// unifdeck's own ReleaseNotesModal) — the OK button ends up in its correct,
// native position for free instead of being awkwardly placed by us.
export function ReleaseNotesModal({
  version,
  date,
  notes,
  closeModal,
}: ReleaseNotesModalProps) {
  const { t } = useTranslation();
  return (
    <ConfirmModal
      strTitle={version}
      strOKButtonText={t("close_button")}
      onOK={closeModal}
      onCancel={closeModal}
    >
      <div style={{ fontSize: 11, opacity: 0.6, marginBottom: 10 }}>
        {formatDate(date)}
      </div>
      {/* ScrollPanel/ScrollPanelGroup was a dead end. The actual mechanism
          (confirmed from unifdeck's own working GameAchievementsModal): a
          Focusable with no onActivate is a "pass-through", not a real focus
          stop, so the d-pad/stick has nothing to step onto — and nothing
          scrolls a plain CSS overflow:auto container on its own regardless.
          Each line needs onActivate (to become a real leaf focus target)
          AND onFocus calling the native scrollIntoView — that's what
          actually moves the viewport as focus steps through the list. */}
      <div
        style={{
          maxHeight: 320,
          overflowY: "auto",
          padding: "12px 16px",
          background: "rgba(0, 0, 0, 0.25)",
          borderRadius: 6,
          fontSize: 13,
        }}
      >
        {notes.length > 0 ? (
          notes.map((n, i) => (
            <Focusable
              key={i}
              onActivate={() => {}}
              onFocus={(e: any) =>
                e.currentTarget.scrollIntoView({ block: "nearest" })
              }
              style={{ marginBottom: 10, lineHeight: 1.4 }}
            >
              {"• "}
              {n}
            </Focusable>
          ))
        ) : (
          <div style={{ opacity: 0.7 }}>—</div>
        )}
      </div>
    </ConfirmModal>
  );
}
