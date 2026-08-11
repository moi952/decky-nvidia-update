import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import enUS from "./locales/en-US.json";
import frFR from "./locales/fr-FR.json";
import esES from "./locales/es-ES.json";

const resources = {
  "en-US": { translation: enUS },
  "fr-FR": { translation: frFR },
  "es-ES": { translation: esES },
};

export const loadTranslations = () => {
  i18n.use(initReactI18next).init({
    resources,
    lng: navigator.language,
    fallbackLng: {
      fr: ["fr-FR"],
      es: ["es-ES"],
      en: ["en-US"],
      default: ["en-US"],
    },
    load: "languageOnly",
    interpolation: { escapeValue: false },
    // Resources are provided directly (no backend fetch), so init can and
    // should complete synchronously. Without this, i18next's default
    // async tick can leave useTranslation() briefly "not ready" on first
    // render, which — combined with react-i18next's Suspense handling —
    // can cause the component to remount once ready, silently resetting
    // any local state (this is what broke the version dropdown selection
    // after i18n was added).
    initImmediate: false,
  });
};
