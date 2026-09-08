import { render, screen, userEvent } from "@/test/utils";
import { FilterSelect } from "@/components/FilterSelect";

describe("FilterSelect", () => {
  const OPTIONS = [
    { value: "active", label: "Active" },
    { value: "archived", label: "Archived" },
  ];

  it("renders a default 'All {label}' option plus every option, aria-labelled by field", () => {
    render(
      <FilterSelect
        value=""
        onChange={vi.fn()}
        options={OPTIONS}
        label="Status"
      />,
    );
    const select = screen.getByRole("combobox", { name: "Filter by status" });
    expect(select).toHaveDisplayValue("All status");
    expect(screen.getByRole("option", { name: "Active" })).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "Archived" }),
    ).toBeInTheDocument();
  });

  it("supports overriding the default 'All' option label", () => {
    render(
      <FilterSelect
        value=""
        onChange={vi.fn()}
        options={OPTIONS}
        label="Status"
        allLabel="Everything"
      />,
    );
    expect(screen.getByRole("combobox")).toHaveDisplayValue("Everything");
  });

  it("reflects the current value as the selected option", () => {
    render(
      <FilterSelect
        value="archived"
        onChange={vi.fn()}
        options={OPTIONS}
        label="Status"
      />,
    );
    expect(screen.getByRole("combobox")).toHaveDisplayValue("Archived");
  });

  it("calls onChange with the newly selected value", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <FilterSelect
        value=""
        onChange={onChange}
        options={OPTIONS}
        label="Status"
      />,
    );

    await user.selectOptions(screen.getByRole("combobox"), "active");
    expect(onChange).toHaveBeenCalledWith("active");
  });
});
