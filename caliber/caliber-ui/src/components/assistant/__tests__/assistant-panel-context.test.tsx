import { render, screen, userEvent } from "@/test/utils";
import { describe, expect, it } from "vitest";

import {
  ASSISTANT_PANEL_COLLAPSED_WIDTH,
  ASSISTANT_PANEL_DEFAULT_WIDTH,
  ASSISTANT_PANEL_MAX_WIDTH,
  ASSISTANT_PANEL_MIN_WIDTH,
  AssistantPanelProvider,
  useAssistantPanel,
} from "@/components/assistant/AssistantPanelContext";


const PANEL_WIDTH_KEY = "caliber.assistant.panel.width";
const PANEL_OPEN_KEY = "caliber.assistant.panel.open";
const PANEL_COLLAPSED_KEY = "caliber.assistant.panel.collapsed";


function renderHarness(): void {
  function Harness(): JSX.Element {
    const panel = useAssistantPanel();
    return (
      <div>
        <div data-testid="open">{String(panel.open)}</div>
        <div data-testid="collapsed">{String(panel.collapsed)}</div>
        <div data-testid="panel-width">{String(panel.panelWidth)}</div>
        <div data-testid="effective-width">{String(panel.effectiveWidth)}</div>
        <button type="button" onClick={panel.toggle}>
          Toggle
        </button>
        <button type="button" onClick={panel.close}>
          Close
        </button>
        <button type="button" onClick={panel.collapse}>
          Collapse
        </button>
        <button type="button" onClick={panel.expand}>
          Expand
        </button>
        <button type="button" onClick={panel.toggleCollapsed}>
          Toggle collapsed
        </button>
        <button type="button" onClick={() => panel.setPanelWidth(200)}>
          Width 200
        </button>
        <button type="button" onClick={() => panel.setPanelWidth(480)}>
          Width 480
        </button>
        <button type="button" onClick={() => panel.setPanelWidth(999)}>
          Width 999
        </button>
      </div>
    );
  }

  render(
    <AssistantPanelProvider>
      <Harness />
    </AssistantPanelProvider>,
  );
}


describe("AssistantPanelContext", () => {
  it("starts closed and uncollapsed with the default width", () => {
    renderHarness();
    expect(screen.getByTestId("open")).toHaveTextContent("false");
    expect(screen.getByTestId("collapsed")).toHaveTextContent("false");
    expect(screen.getByTestId("panel-width")).toHaveTextContent(
      String(ASSISTANT_PANEL_DEFAULT_WIDTH),
    );
    expect(screen.getByTestId("effective-width")).toHaveTextContent(
      String(ASSISTANT_PANEL_DEFAULT_WIDTH),
    );
  });

  it.each([
    ["200", String(ASSISTANT_PANEL_MIN_WIDTH)],
    ["480", "480"],
    ["999", String(ASSISTANT_PANEL_MAX_WIDTH)],
    ["not-a-number", String(ASSISTANT_PANEL_DEFAULT_WIDTH)],
    ["", String(ASSISTANT_PANEL_MIN_WIDTH)],
  ])("hydrates persisted width %s -> %s", (stored, expectedWidth) => {
    window.localStorage.setItem(PANEL_WIDTH_KEY, stored);
    renderHarness();
    expect(screen.getByTestId("panel-width")).toHaveTextContent(expectedWidth);
  });

  it("hydrates persisted open and collapsed state", () => {
    window.localStorage.setItem(PANEL_OPEN_KEY, "true");
    window.localStorage.setItem(PANEL_COLLAPSED_KEY, "true");
    renderHarness();
    expect(screen.getByTestId("open")).toHaveTextContent("true");
    expect(screen.getByTestId("collapsed")).toHaveTextContent("true");
    expect(screen.getByTestId("effective-width")).toHaveTextContent(
      String(ASSISTANT_PANEL_COLLAPSED_WIDTH),
    );
  });

  it("toggle opens the panel and clears the collapsed flag", async () => {
    window.localStorage.setItem(PANEL_COLLAPSED_KEY, "true");
    renderHarness();
    await userEvent.click(screen.getByText("Toggle"));
    expect(screen.getByTestId("open")).toHaveTextContent("true");
    expect(screen.getByTestId("collapsed")).toHaveTextContent("false");
    expect(window.localStorage.getItem(PANEL_OPEN_KEY)).toBe("true");
    expect(window.localStorage.getItem(PANEL_COLLAPSED_KEY)).toBe("false");
  });

  it("close resets open and collapsed state", async () => {
    window.localStorage.setItem(PANEL_OPEN_KEY, "true");
    window.localStorage.setItem(PANEL_COLLAPSED_KEY, "true");
    renderHarness();
    await userEvent.click(screen.getByText("Close"));
    expect(screen.getByTestId("open")).toHaveTextContent("false");
    expect(screen.getByTestId("collapsed")).toHaveTextContent("false");
    expect(window.localStorage.getItem(PANEL_OPEN_KEY)).toBe("false");
    expect(window.localStorage.getItem(PANEL_COLLAPSED_KEY)).toBe("false");
  });

  it("collapse and expand adjust the effective width", async () => {
    window.localStorage.setItem(PANEL_OPEN_KEY, "true");
    renderHarness();
    await userEvent.click(screen.getByText("Collapse"));
    expect(screen.getByTestId("collapsed")).toHaveTextContent("true");
    expect(screen.getByTestId("effective-width")).toHaveTextContent(
      String(ASSISTANT_PANEL_COLLAPSED_WIDTH),
    );

    await userEvent.click(screen.getByText("Expand"));
    expect(screen.getByTestId("collapsed")).toHaveTextContent("false");
    expect(screen.getByTestId("effective-width")).toHaveTextContent(
      String(ASSISTANT_PANEL_DEFAULT_WIDTH),
    );
  });

  it("toggleCollapsed flips the collapsed state", async () => {
    renderHarness();
    await userEvent.click(screen.getByText("Toggle collapsed"));
    expect(screen.getByTestId("collapsed")).toHaveTextContent("true");
    await userEvent.click(screen.getByText("Toggle collapsed"));
    expect(screen.getByTestId("collapsed")).toHaveTextContent("false");
  });

  it.each([
    ["Width 200", String(ASSISTANT_PANEL_MIN_WIDTH)],
    ["Width 480", "480"],
    ["Width 999", String(ASSISTANT_PANEL_MAX_WIDTH)],
  ])("setPanelWidth clamps through %s", async (buttonLabel, expectedWidth) => {
    renderHarness();
    await userEvent.click(screen.getByText(buttonLabel));
    expect(screen.getByTestId("panel-width")).toHaveTextContent(expectedWidth);
    expect(window.localStorage.getItem(PANEL_WIDTH_KEY)).toBe(expectedWidth);
  });

  it("updates the CSS custom property when the panel opens", async () => {
    renderHarness();
    await userEvent.click(screen.getByText("Toggle"));
    expect(
      document.documentElement.style.getPropertyValue("--assistant-panel-width"),
    ).toBe(`${ASSISTANT_PANEL_DEFAULT_WIDTH}px`);
  });

  it("updates the CSS custom property when the panel collapses", async () => {
    window.localStorage.setItem(PANEL_OPEN_KEY, "true");
    renderHarness();
    await userEvent.click(screen.getByText("Collapse"));
    expect(
      document.documentElement.style.getPropertyValue("--assistant-panel-width"),
    ).toBe(`${ASSISTANT_PANEL_COLLAPSED_WIDTH}px`);
  });

  it("sets the CSS custom property to 0px when the panel closes", async () => {
    window.localStorage.setItem(PANEL_OPEN_KEY, "true");
    renderHarness();
    await userEvent.click(screen.getByText("Close"));
    expect(
      document.documentElement.style.getPropertyValue("--assistant-panel-width"),
    ).toBe("0px");
  });
});
