/**
 * Sanitizing ATS-supplied description HTML before it is rendered.
 *
 * Phase 2A rendered `description_html` straight into the DOM via
 * `dangerouslySetInnerHTML`, with a comment noting it as acceptable for a
 * personal same-origin tool. It is not acceptable any more, for a reason that
 * changed rather than a standard that rose: the board now ingests **aggregator
 * feeds** — Himalayas, Remotive and Wellfound — where the HTML is written by
 * whoever posted the job, not by a vetted employer's ATS. That is
 * attacker-controlled markup arriving over the network and being executed in
 * a page that can reach the backend API on the same origin.
 *
 * No dependency, on purpose
 * -------------------------
 * DOMPurify is the obvious answer and would be fine, but this needs an
 * allowlist over a fixed, narrow vocabulary — JDs are headings, lists, links
 * and emphasis — and an allowlist that small is more auditable inline than as
 * a configured third-party policy. It also keeps the bundle free of a
 * security dependency that then has to be kept current.
 *
 * The approach is allowlist-only: parse into an inert document, walk it, and
 * delete anything not explicitly permitted. A blocklist ("strip <script>") is
 * the wrong shape — it fails open on everything its author did not think of,
 * and the interesting vectors are never the obvious tag.
 */

/** Tags a job description legitimately uses. Everything else is unwrapped. */
const ALLOWED_TAGS = new Set([
  "p", "br", "hr", "div", "span", "section",
  "h1", "h2", "h3", "h4", "h5", "h6",
  "ul", "ol", "li",
  "strong", "b", "em", "i", "u", "s", "code", "pre", "blockquote",
  "a", "table", "thead", "tbody", "tr", "th", "td",
]);

/** Attributes kept, per tag. There is no global allowlist — an attribute has
 *  to be named against the tag that may carry it. */
const ALLOWED_ATTRS: Record<string, Set<string>> = {
  a: new Set(["href", "title"]),
  td: new Set(["colspan", "rowspan"]),
  th: new Set(["colspan", "rowspan"]),
};

/** Tags removed along with their contents, rather than unwrapped.
 *  Unwrapping `<script>` would dump its source into the page as text. */
const DROP_ENTIRELY = new Set([
  "script", "style", "iframe", "object", "embed", "link", "meta",
  "form", "input", "button", "select", "textarea", "svg", "math",
  "noscript", "template", "base", "frame", "frameset", "applet", "audio",
  "video", "source", "track", "canvas", "portal",
]);

/** Schemes an href may use. */
const SAFE_SCHEMES = new Set(["http", "https", "mailto", "tel"]);

/**
 * Strip every character a browser ignores while resolving a URL scheme.
 *
 * Written as a code-point filter rather than a regex character class on
 * purpose: the set includes C0 controls and assorted Unicode spaces, and
 * expressing those as escapes inside a regex literal is exactly the sort of
 * thing that survives review and then turns out to have been mangled by an
 * editor or a build step. A numeric comparison cannot be mis-encoded.
 */
function stripIgnorable(value: string): string {
  let out = "";
  for (const char of value) {
    const code = char.codePointAt(0) ?? 0;
    const ignorable =
      code <= 0x20 || // C0 controls and space — covers tab, newline, NUL
      code === 0x7f ||
      code === 0xa0 ||
      code === 0x1680 ||
      (code >= 0x2000 && code <= 0x200d) ||
      code === 0x2028 ||
      code === 0x2029 ||
      code === 0x202f ||
      code === 0x205f ||
      code === 0x3000 ||
      code === 0xfeff;
    if (!ignorable) out += char;
  }
  return out;
}

/**
 * Is this URL safe to keep in an href?
 *
 * The check runs on the ignorable-stripped value, because `java<TAB>script:`
 * and a leading NUL are the standard ways past a naive
 * `startsWith("javascript:")` test — a browser resolves those to
 * `javascript:` while a string comparison does not.
 *
 * The rule fails CLOSED, which is the correction to an earlier version that
 * did not. That version matched `^[a-z][a-z0-9+.-]*:` and, on no match, fell
 * through to "no scheme, therefore relative, therefore fine". A live test
 * against jsdom caught the hole: the HTML parser rewrites a NUL in an
 * attribute to U+FFFD per spec, so `<a href="<NUL>javascript:...">` arrives
 * as `�javascript:...`. That is not ignorable whitespace, so it did not
 * get stripped; it is also not a valid scheme character, so the regex did not
 * match — and the value was waved through as a relative link.
 *
 * So anything that looks like it is *reaching for* a scheme — a colon before
 * the first `/`, `?` or `#` — must name an allowlisted one. Only a value with
 * no such colon is treated as relative.
 */
export function safeUrl(value: string): boolean {
  const cleaned = stripIgnorable(value);
  if (!cleaned) return false;

  // Where the path/query/fragment begins; a colon after any of these is data
  // (`/a/b:c`, `?q=a:b`) rather than a scheme.
  const authorityStart = cleaned.search(/[/?#]/);
  const colon = cleaned.indexOf(":");
  const reachingForScheme = colon !== -1 && (authorityStart === -1 || colon < authorityStart);

  if (!reachingForScheme) return true; // genuinely relative, or a fragment

  // It is claiming a scheme, so it has to name one we allow — exactly, with
  // no stray characters smuggled in.
  const scheme = cleaned.slice(0, colon).toLowerCase();
  return /^[a-z][a-z0-9+.-]*$/.test(scheme) && SAFE_SCHEMES.has(scheme);
}

/**
 * Return a sanitized copy of `html`, safe for `dangerouslySetInnerHTML`.
 *
 * Parsing happens via `DOMParser`, which builds an **inert** document:
 * scripts do not execute and `<img onerror>` does not fire, unlike assigning
 * to a live element's `innerHTML`. That inertness is what makes it safe to
 * inspect the tree before deciding what to keep.
 */
export function sanitizeHtml(html: string): string {
  if (!html) return "";

  // No DOMParser (SSR, an exotic runtime): fall back to stripped text, never
  // to raw HTML. Degrading to unsanitized markup would defeat the module.
  if (typeof DOMParser === "undefined") {
    return html.replace(/<[^>]*>/g, "");
  }

  const doc = new DOMParser().parseFromString(html, "text/html");

  const walk = (node: Element): void => {
    // Snapshot the children: the loop mutates the tree as it goes.
    for (const child of Array.from(node.children)) {
      const tag = child.tagName.toLowerCase();

      if (DROP_ENTIRELY.has(tag)) {
        child.remove();
        continue;
      }

      if (!ALLOWED_TAGS.has(tag)) {
        // Unwrap: keep the text and children, discard the element. A JD
        // wrapped in an unknown tag should lose its wrapper, not its content.
        walk(child);
        child.replaceWith(...Array.from(child.childNodes));
        continue;
      }

      const permitted = ALLOWED_ATTRS[tag];
      for (const attr of Array.from(child.attributes)) {
        const name = attr.name.toLowerCase();
        // Anything unnamed goes, which is what removes every `on*` handler
        // without needing to enumerate them.
        if (!permitted?.has(name)) {
          child.removeAttribute(attr.name);
          continue;
        }
        if (name === "href" && !safeUrl(attr.value)) {
          child.removeAttribute(attr.name);
        }
      }

      // A surviving link leaves our origin, so it must not get a window
      // handle back to it.
      if (tag === "a" && child.getAttribute("href")) {
        child.setAttribute("target", "_blank");
        child.setAttribute("rel", "noopener noreferrer nofollow");
      }

      walk(child);
    }
  };

  walk(doc.body);
  return doc.body.innerHTML;
}
