import { describe, expect, it } from "vitest";

import type { Product } from "@/types/api";
import { groupByBrand } from "./SocialPanel";

const product = (id: string, brand: string) => ({ id, brand }) as Product;

describe("groupByBrand", () => {
  it("puts one brand's products in one slot, in first-seen order, ignoring case", () => {
    const groups = groupByBrand([
      product("1", "Huawei"),
      product("2", "Oura"),
      product("3", "huawei "),
      product("4", "Huawei"),
    ]);
    expect(groups.map((g) => g.map((p) => p.id))).toEqual([["1", "3", "4"], ["2"]]);
  });
});
