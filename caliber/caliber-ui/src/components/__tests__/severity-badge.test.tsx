import { render, screen } from "@/test/utils";
import { SeverityBadge } from "@/components/SeverityBadge";

describe("SeverityBadge", () => {
  it("renders critical in red, with an accessible label naming the severity", () => {
    render(<SeverityBadge severity="critical" />);
    const badge = screen.getByLabelText("Critical severity");
    expect(badge).toHaveTextContent("Critical");
    expect(badge.className).toContain("bg-red-100");
  });

  it("renders standard in gray, with an accessible label naming the severity", () => {
    render(<SeverityBadge severity="standard" />);
    const badge = screen.getByLabelText("Standard severity");
    expect(badge).toHaveTextContent("Standard");
    expect(badge.className).toContain("bg-gray-100");
  });
});
