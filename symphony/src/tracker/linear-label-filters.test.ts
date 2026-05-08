import { describe, expect, it } from "vitest";
import { issueMatchesTrackerFilters } from "./linear.js";

describe("issueMatchesTrackerFilters", () => {
  it("allows all when exclude and require_any are empty", () => {
    expect(issueMatchesTrackerFilters(["foo"], { exclude_labels: [], require_any_labels: [] })).toBe(true);
  });

  it("excludes when any label matches exclude_labels", () => {
    expect(
      issueMatchesTrackerFilters(["runner:mac-mini", "x"], {
        exclude_labels: ["runner:mac-mini"],
        require_any_labels: [],
      }),
    ).toBe(false);
  });

  it("requires at least one require_any_labels when set", () => {
    expect(
      issueMatchesTrackerFilters(["misc"], {
        exclude_labels: [],
        require_any_labels: ["runner:mac-mini"],
      }),
    ).toBe(false);
    expect(
      issueMatchesTrackerFilters(["runner:mac-mini"], {
        exclude_labels: [],
        require_any_labels: ["runner:mac-mini"],
      }),
    ).toBe(true);
  });

  it("applies exclude before require semantics via combined predicate", () => {
    expect(
      issueMatchesTrackerFilters(["runner:mac-mini"], {
        exclude_labels: ["draft"],
        require_any_labels: ["runner:mac-mini"],
      }),
    ).toBe(true);
    expect(
      issueMatchesTrackerFilters(["runner:mac-mini", "draft"], {
        exclude_labels: ["draft"],
        require_any_labels: ["runner:mac-mini"],
      }),
    ).toBe(false);
  });
});
