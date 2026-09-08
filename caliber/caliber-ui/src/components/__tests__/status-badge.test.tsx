import { render, screen } from "@/test/utils";
import { StatusBadge } from "@/components/StatusBadge";

describe("StatusBadge", () => {
  it.each([
    ["pending", "Pending", "bg-amber-100"],
    ["verified", "Verified", "bg-green-100"],
    ["dismissed", "Dismissed", "bg-gray-100"],
    ["duplicate", "Duplicate", "bg-gray-100"],
    ["queued", "Queued", "bg-blue-100"],
    ["running", "Running", "bg-blue-100"],
    ["awaiting_approval", "Awaiting Approval", "bg-amber-100"],
    ["approved", "Approved", "bg-green-100"],
    ["completed", "Completed", "bg-green-100"],
    ["rejected", "Rejected", "bg-red-100"],
    ["failed", "Failed", "bg-red-100"],
    ["request_changes", "Changes Requested", "bg-amber-100"],
  ])("renders %s as %s with its tone color", (status, label, toneClass) => {
    render(<StatusBadge status={status} />);
    const badge = screen.getByText(label);
    expect(badge).toBeInTheDocument();
    expect(badge.className).toContain(toneClass);
  });

  it("falls back to the raw status string and a neutral tone for an unrecognized status", () => {
    render(<StatusBadge status="some_future_status" />);
    const badge = screen.getByText("some_future_status");
    expect(badge).toBeInTheDocument();
    expect(badge.className).toContain("bg-gray-100");
    expect(badge.className).toContain("text-gray-700");
  });
});
