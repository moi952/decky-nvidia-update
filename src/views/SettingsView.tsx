import React from "react";
import { Focusable, PanelSection } from "@decky/ui";
import { ActionButton } from "@moi952/decky-ui-kit";
import {
  BackHandler,
  UpdateHistorySection,
  GitHubSection,
  SupportSection,
  getWhatsNewVersions,
} from "@moi952/decky-plugin-toolkit";
import { useTranslation } from "react-i18next";
import { FiArrowLeft } from "react-icons/fi";

import { fetchPluginReleases } from "../utils/githubReleases";
import { FEATURE_REQUEST_URL, BUG_REPORT_URL } from "../utils/links";
import { HelpScope, helpProps } from "../ContextHelp";

interface SettingsViewProps {
  onBack: () => void;
  pluginUpdateBlocked: boolean;
}

// Just picks which of @moi952/decky-plugin-toolkit's ready-made Settings
// sections this plugin wants, in what order — each section owns its own
// state, focus-restore and translations internally. Driver-related settings
// (build numbers, channel, channel filters) stay on the main panel, not here.
export const SettingsView: React.FC<SettingsViewProps> = ({
  onBack,
  pluginUpdateBlocked,
}) => {
  const { t } = useTranslation("settings");

  return (
    <BackHandler onBack={onBack}>
      <PanelSection>
        <Focusable
          style={{ display: "flex", alignItems: "center", gap: "8px" }}
          flow-children="horizontal"
        >
          <ActionButton onClick={onBack} {...helpProps("settings")}>
            <FiArrowLeft size={16} />
          </ActionButton>
          <span style={{ fontWeight: 600 }}>{t("settings_title")}</span>
        </Focusable>
      </PanelSection>

      <HelpScope topic="history">
        <UpdateHistorySection versions={getWhatsNewVersions()} />
      </HelpScope>
      {pluginUpdateBlocked ? (
        <PanelSection>
          <p>{t("plugin_update_wait")}</p>
        </PanelSection>
      ) : (
        <HelpScope topic="plugin">
          <GitHubSection
            fetchReleases={fetchPluginReleases}
            featureRequestUrl={FEATURE_REQUEST_URL}
            bugReportUrl={BUG_REPORT_URL}
          />
        </HelpScope>
      )}
      <HelpScope topic="support">
        <SupportSection />
      </HelpScope>
    </BackHandler>
  );
};
