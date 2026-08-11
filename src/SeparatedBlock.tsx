import type { ReactNode, CSSProperties } from "react";

// Custom (non-Field/ToggleField) content blocks don't get Steam's own
// standard row spacing/separator for free — this reproduces it in one
// place instead of re-guessing the same margins/border on every block.
const TOP_SPACE = 8;
const BOTTOM_SPACE = 6;
const BORDER: CSSProperties["borderBottom"] = "1px solid rgba(255,255,255,0.1)";

interface SeparatedBlockProps {
  children: ReactNode;
  bottomBorder?: boolean;
}

export function SeparatedBlock({
  children,
  bottomBorder = true,
}: SeparatedBlockProps) {
  return (
    <div
      style={{
        marginTop: TOP_SPACE,
        ...(bottomBorder
          ? { paddingBottom: BOTTOM_SPACE, borderBottom: BORDER }
          : {}),
      }}
    >
      {children}
    </div>
  );
}
