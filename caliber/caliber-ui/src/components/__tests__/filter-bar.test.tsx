import { render, screen } from "@/test/utils";
import { FilterBar } from "@/components/FilterBar";

describe("FilterBar", () => {
  it("renders nothing but the bar shell when no slots are given", () => {
    render(<FilterBar />);
    const bar = screen.getByTestId("filter-bar");
    expect(bar).toBeInTheDocument();
    expect(bar).toBeEmptyDOMElement();
  });

  it("renders the search slot alone", () => {
    render(<FilterBar search={<input placeholder="Search…" />} />);
    expect(screen.getByPlaceholderText("Search…")).toBeInTheDocument();
    expect(screen.queryByTestId("clear-filters")).not.toBeInTheDocument();
  });

  it("renders filters and actions slots alongside search", () => {
    render(
      <FilterBar
        search={<input placeholder="Search…" />}
        filters={<button data-testid="status-filter">Status</button>}
        actions={<button data-testid="clear-filters">Clear</button>}
      />,
    );
    expect(screen.getByPlaceholderText("Search…")).toBeInTheDocument();
    expect(screen.getByTestId("status-filter")).toBeInTheDocument();
    expect(screen.getByTestId("clear-filters")).toBeInTheDocument();
  });

  it("merges a caller className onto the card classes", () => {
    render(<FilterBar className="mt-4" />);
    expect(screen.getByTestId("filter-bar").className).toContain("mt-4");
  });
});
