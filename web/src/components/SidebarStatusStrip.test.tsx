import { describe, expect, it } from "vitest";
import { gatewayLine } from "./SidebarStatusStrip";

const translations = {
  app: {
    gatewayStrip: {
      running: "Running",
      starting: "Starting",
      failed: "Failed",
      stopped: "Stopped",
      off: "Off",
    },
  },
};

describe("gatewayLine", () => {
  it("shows the live fleet when the global profile has no gateway", () => {
    const line = gatewayLine(
      {
        gateway_running: false,
        gateway_state: "stopped",
        gateway_count: 22,
      } as never,
      translations as never,
    );

    expect(line).toEqual({ label: "Running (22)", tone: "text-success" });
  });

  it("preserves the current gateway state when no profile gateways are live", () => {
    const line = gatewayLine(
      {
        gateway_running: false,
        gateway_state: "stopped",
        gateway_count: 0,
      } as never,
      translations as never,
    );

    expect(line).toEqual({ label: "Stopped", tone: "text-muted-foreground" });
  });
});
