import { findModuleExport, getFocusNavController } from "@decky/ui";
import { useLayoutEffect } from "react";

type Action = {
  label?: { props?: Record<string, unknown> };
  props?: Record<string, unknown>;
};
type Actions = Record<number, Action | undefined>;
type ActionStore = { SetActionsFromMap(map: Actions): void };
const nativeButtons = findModuleExport(
  (value) =>
    value &&
    typeof value === "object" &&
    "A" in value &&
    "RightStick" in value &&
    "LeftStickClick" in value,
);

export const stickHint = (label: string) => (
  <span data-nvidia-scroll-hint>{label}</span>
);

// Like FileExplorer, keep Steam's native footer and controller glyph renderer.
// Its generic action converter maps unsupported stick actions to A. Correct
// only our marked hint, and restore the original store when this dialog closes.
export function useScrollHint() {
  useLayoutEffect(() => {
    const steam = (
      window as unknown as {
        SteamUIStore?: {
          GetFocusedWindowInstance(): { ActionDescriptionStore?: ActionStore };
        };
      }
    ).SteamUIStore;
    const store = steam?.GetFocusedWindowInstance()?.ActionDescriptionStore;
    if (!store || !nativeButtons) return;
    const original = store.SetActionsFromMap;
    const descriptor = Object.getOwnPropertyDescriptor(
      store,
      "SetActionsFromMap",
    );
    if (descriptor && !descriptor.configurable) return;
    const wrapper = (map: Actions) => {
      const hint = map[nativeButtons.A];
      if (
        (hint?.label?.props || hint?.props)?.["data-nvidia-scroll-hint"] ===
        undefined
      ) {
        original.call(store, map);
        return;
      }
      const nav = getFocusNavController() as unknown as {
        GetActiveNavTree(): {
          GetLastFocusedNode(): { GetActiveActionDescriptions(): Actions };
        };
      };
      const active = nav
        .GetActiveNavTree()
        ?.GetLastFocusedNode()
        ?.GetActiveActionDescriptions();
      if (!active) {
        original.call(store, map);
        return;
      }
      const fixed = {
        ...map,
        [nativeButtons.A]: active[1],
      };
      for (const [button, native] of [
        [15, nativeButtons.LeftStickClick],
        [18, nativeButtons.RightStick],
      ]) {
        const action = active[button];
        if (
          (action?.label?.props || action?.props)?.[
            "data-nvidia-scroll-hint"
          ] !== undefined
        )
          fixed[native] = action;
      }
      original.call(store, fixed);
    };
    Object.defineProperty(store, "SetActionsFromMap", {
      configurable: true,
      value: wrapper,
    });
    return () => {
      if (store.SetActionsFromMap !== wrapper) return;
      if (descriptor)
        Object.defineProperty(store, "SetActionsFromMap", descriptor);
      else delete (store as Partial<ActionStore>).SetActionsFromMap;
    };
  }, []);
}
