import { findModuleExport } from "@decky/ui";
import { useEffect, type RefObject } from "react";

type Subscription = { unregister(): void };
type AnalogInput = {
  RegisterForControllerAnalogInputMessages(
    callback: (
      controller: number,
      button: number,
      pressed: boolean,
      x: number,
      y: number,
    ) => void,
  ): Subscription;
  EnableControllerAnalogInputMessages(enabled: boolean): void;
};
const buttons = findModuleExport(
  (value) =>
    value &&
    typeof value === "object" &&
    "GAMEPAD_ANALOG_SCROLL" in value &&
    "GAMEPAD_RIGHTSTICK_CLICK" in value,
);

// Steam's current analog callback uses five arguments, unlike the old ui types.
// Listen only while this viewer is focused in history mode. Do not change the
// controller's layout or keyboard action set.
export function useControllerScroll(
  viewport: RefObject<HTMLDivElement | null>,
  enabled: boolean,
) {
  useEffect(() => {
    const input = (
      window as unknown as { SteamClient?: { Input?: AnalogInput } }
    ).SteamClient?.Input;
    if (
      !enabled ||
      !input?.RegisterForControllerAnalogInputMessages ||
      !buttons
    )
      return;
    const positions = new Map<number, number>();
    let pending = 0;
    let frame = 0;
    let disposed = false;
    const subscription = input.RegisterForControllerAnalogInputMessages(
      (controller, button, _pressed, _x, y) => {
        if (
          disposed ||
          button !== buttons.GAMEPAD_ANALOG_SCROLL ||
          !Number.isFinite(y)
        )
          return;
        // Steam reports a cumulative scroll position, not a stick deflection or
        // per-message delta. Start each controller at its own current baseline.
        const previous = positions.get(controller);
        positions.set(controller, y);
        if (previous === undefined) return;
        const delta = y - previous;
        if (Math.abs(delta) > 1024) return; // Reset/discontinuity, not normal input.
        pending += delta * 0.35;
        if (!frame)
          frame = requestAnimationFrame(() => {
            frame = 0;
            const element = viewport.current;
            if (!disposed && element) element.scrollTop += pending;
            pending = 0;
          });
      },
    );
    input.EnableControllerAnalogInputMessages(true);
    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      subscription.unregister();
      input.EnableControllerAnalogInputMessages(false);
    };
  }, [viewport, enabled]);
}
