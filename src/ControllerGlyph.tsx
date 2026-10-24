import { findModuleExport } from "@decky/ui";
import type { ComponentType } from "react";

const NativeGlyph = findModuleExport(
  (value) =>
    typeof value === "function" &&
    value.toString().includes("actionButton:") &&
    value.toString().includes("NavigationSourceGlyphInfo"),
) as ComponentType<{ actionButton: number }> | undefined;
const nativeButtons = findModuleExport(
  (value) =>
    value &&
    typeof value === "object" &&
    "RightStick" in value &&
    "LeftStickClick" in value,
);

export function ControllerGlyph({ click = false }: { click?: boolean }) {
  const button = nativeButtons?.[click ? "LeftStickClick" : "RightStick"];
  return (
    <span
      aria-label={click ? "Press left stick" : "Right stick"}
      role="img"
      style={{
        display: "inline-flex",
        width: 26,
        height: 26,
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      {NativeGlyph && button !== undefined ? (
        <NativeGlyph actionButton={button} />
      ) : (
        <b>{click ? "L3" : "R"}</b>
      )}
    </span>
  );
}
