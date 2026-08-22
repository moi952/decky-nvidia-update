import { DialogButton, Focusable, ModalRoot, showModal } from "@decky/ui";
import { useTranslation } from "react-i18next";

interface RootfsSpaceWarningModalContentProps {
  totalMb: number;
  onConfirm: () => void;
  onClose: () => void;
}

// Same shape as decky-proton-launch's delete-confirmation modals (e.g.
// ButtonDeleteCustomVariableModal): a plain ModalRoot with a title, a
// description, and a Cancel/Confirm DialogButton row in a Focusable —
// not Decky's built-in ConfirmModal alert dialog.
function RootfsSpaceWarningModalContent({
  totalMb,
  onConfirm,
  onClose,
}: RootfsSpaceWarningModalContentProps) {
  const { t } = useTranslation();
  return (
    <ModalRoot>
      <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
        <div style={{ fontWeight: 600 }}>{t("rootfs_warning_title")}</div>
        <div>
          {t("rootfs_warning_body", {
            size: `${(totalMb / 1024).toFixed(1)} GiB`,
          })}
        </div>
        <Focusable
          style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}
          flow-children="horizontal"
        >
          <DialogButton onClick={onClose}>
            {t("rootfs_warning_cancel")}
          </DialogButton>
          <DialogButton
            onClick={() => {
              onConfirm();
              onClose();
            }}
          >
            {t("rootfs_warning_install_anyway")}
          </DialogButton>
        </Focusable>
      </div>
    </ModalRoot>
  );
}

// Shown before starting an install when this system's rootfs partition
// looks smaller than the documented minimum (README: SteamOS's stock 5GiB
// rootfs-A/B has no headroom for a driver install; needs growing to at
// least 8GiB via steamos-nvidia-installer first). Purely a warning — the
// script's own late-stage space check (right before the real install copy)
// is still the actual safety net; this just avoids spending the whole
// DKMS build only to fail at the very end on an obviously too-small
// partition.
export function openRootfsSpaceWarningModal(
  totalMb: number,
  onConfirm: () => void,
) {
  let modal: ReturnType<typeof showModal> | null = null;
  modal = showModal(
    <RootfsSpaceWarningModalContent
      totalMb={totalMb}
      onConfirm={onConfirm}
      onClose={() => modal?.Close()}
    />,
  );
}
