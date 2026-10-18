import { existsSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { describe, it, expect } from "vitest";
import {
  INSTITUTION_LOGO_DIR,
  INSTITUTION_LOGOS,
  institutionLogo,
} from "@/lib/institution-logos";

const PUBLIC_DIR = path.resolve(__dirname, "../../../public");
const LOGO_DIR = path.join(PUBLIC_DIR, INSTITUTION_LOGO_DIR);
/** The logo files, without dotfiles such as a Finder-written .DS_Store. */
const logoFiles = () => readdirSync(LOGO_DIR).filter((f) => !f.startsWith("."));

describe("institutionLogo", () => {
  it("resolves a known slug to its public path", () => {
    expect(institutionLogo("banco_falabella")).toEqual({
      src: "/logos/institutions/banco_falabella.svg",
      bleed: false,
    });
    expect(institutionLogo("mach")).toEqual({
      src: "/logos/institutions/mach.png",
      bleed: true,
    });
  });

  it.each(["manual", "csv_import", "my_custom_bank", "", "constructor"])(
    "has no logo for %j",
    (slug) => {
      expect(institutionLogo(slug)).toBeNull();
    }
  );
});

describe("INSTITUTION_LOGOS files", () => {
  it.each(Object.entries(INSTITUTION_LOGOS))(
    "%s points at a file under public/",
    (slug) => {
      const { src } = institutionLogo(slug)!;
      expect(existsSync(path.join(PUBLIC_DIR, src))).toBe(true);
    }
  );

  it("leaves no logo file unreferenced", () => {
    const referenced = new Set(
      Object.values(INSTITUTION_LOGOS).map((entry) => entry.file)
    );
    expect(logoFiles().filter((f) => !referenced.has(f))).toEqual([]);
  });

  it("names each file after its slug", () => {
    for (const [slug, { file }] of Object.entries(INSTITUTION_LOGOS)) {
      expect(file).toMatch(new RegExp(`^${slug}\\.(svg|png|webp)$`));
    }
  });
});

// There is no CSP and the files are same-origin, so a script in an SVG opened
// directly would run as the app. Logos must be plain static images.
describe("SVG logos", () => {
  const svgs = logoFiles().filter((f) => f.endsWith(".svg"));
  /** An allowlist: a blocklist misses <set>, <animate> and <h:script>. */
  const STATIC_ELEMENTS = new Set([
    "svg",
    "g",
    "defs",
    "path",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "style",
    "title",
    "desc",
  ]);

  it("exist", () => {
    expect(svgs.length).toBeGreaterThan(0);
  });

  it.each(svgs)("%s uses only static shape elements", (file) => {
    const svg = readFileSync(path.join(LOGO_DIR, file), "utf8");
    // Opening tags only: skips </closing>, <?xml ?> and <!-- comments -->.
    const elements = [...svg.matchAll(/<([^\s/>!?]+)/g)].map((m) => m[1]);
    expect(elements.filter((e) => !STATIC_ELEMENTS.has(e))).toEqual([]);
  });

  it.each(svgs)("%s has no handlers, links or external content", (file) => {
    const svg = readFileSync(path.join(LOGO_DIR, file), "utf8");
    for (const pattern of [
      /\son\w+\s*=/i,
      /href\s*=/i,
      /<!(DOCTYPE|ENTITY)/i,
      /@import/i,
      /url\(/i,
    ]) {
      expect(svg).not.toMatch(pattern);
    }
  });
});
