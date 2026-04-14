# Design System Specification: The Precision Editorial (IndexPulse)

> Source: `/Users/simon/code/stitch-mocks/stitch_url_indexing_manager/slate_teal_precision/DESIGN.md`
> Applied to this project: 2026-04-14

## 1. Overview & Creative North Star: "The Data Architect"
This design system moves away from the cluttered, "dashboard-in-a-box" aesthetic. Our Creative North Star is **The Data Architect**. We treat SEO data not as a chaotic stream, but as a structured, high-end editorial piece. 

The system achieves authority through **intentional white space, tonal layering, and typographic hierarchy**. We break the monotony of data-heavy interfaces by using asymmetrical layouts—where critical insights are given "breathing room" (Display-scale typography) while dense metrics are tucked into sophisticated, borderless containers. The result is an experience that feels like a premium financial journal rather than a generic SaaS tool.

---

## 2. Colors & Surface Philosophy
The palette is rooted in a "Clean Slate" philosophy. We use cool neutrals to recede, allowing our signature Teal and Status colors to act as the primary navigational beacons.

### The "No-Line" Rule
**Explicit Instruction:** Designers are prohibited from using 1px solid borders to section off content. 
*   **How to define boundaries:** Use background shifts. A `surface_container_low` section sitting on a `background` provides all the definition needed. 
*   **Why:** Hard lines create visual "noise" that competes with data text. Tonal shifts create a calmer, more premium "wash" across the screen.

### Surface Hierarchy & Nesting
Treat the UI as a series of physical layers. Use the following tokens to create "nested" depth:
*   **Base:** `background` (#f7f9fb) – The canvas.
*   **Secondary Sections:** `surface_container_low` (#f2f4f6) – For sidebars or utility panels.
*   **Primary Data Cards:** `surface_container_lowest` (#ffffff) – For the highest contrast against the slate text.
*   **Nesting:** When placing a search bar inside a white card, use `surface_container` (#eceef0) to "recess" the input field into the card.

### The "Glass & Gradient" Rule
To elevate the "Professional" aesthetic into "High-End," use **Glassmorphism** for floating elements (Modals, Popovers, Floating Action Buttons). 
*   **Implementation:** Use `surface` colors at 80% opacity with a `20px` backdrop-blur.
*   **Signature Textures:** For primary CTAs and Quota Progress bars, use a subtle linear gradient: `primary` (#006260) to `primary_container` (#0b7d7a) at a 135-degree angle. This adds "soul" and dimension to an otherwise flat interface.

---

## 3. Typography: Editorial Authority
We pair **Manrope** (for structural headlines) with **Inter** (for data density). Manrope provides a modern, geometric authority, while Inter’s high x-height ensures readability in complex SEO tables.

*   **Display (Manrope):** Large, airy, and confident. Used for high-level "Pulse" metrics (e.g., total indexed pages).
*   **Headline/Title (Manrope):** Used for section headers. Bold weights (600+) are required to establish hierarchy without needing lines.
*   **Body (Inter):** The workhorse. Use `body-md` (0.875rem) as the standard for table data to maximize information density without sacrificing legibility.
*   **Label (Inter):** Use `label-sm` (0.6875rem) in All Caps with +0.05em letter spacing for table headers. This distinguishes metadata from actual data values.

---

## 4. Elevation & Depth
We eschew traditional drop shadows for **Tonal Layering**.

*   **The Layering Principle:** Depth is achieved by "stacking." A `surface_container_lowest` card placed on a `surface_container_low` background creates a natural lift.
*   **Ambient Shadows:** For elements that truly "float" (like tooltips or dropdowns), use an extra-diffused shadow: `0px 12px 32px rgba(30, 41, 59, 0.05)`. Note the color: we use a 5% opacity of our "Ink" (`on_surface`) color, never pure black.
*   **The "Ghost Border":** If a container requires further definition (e.g., high-density data cells), use the `outline_variant` token at **15% opacity**. This creates a "suggestion" of a boundary rather than a hard wall.

---

## 5. Components

### Buttons
*   **Primary:** Gradient fill (`primary` to `primary_container`), `DEFAULT` (8px) roundedness, no border. White text.
*   **Secondary:** `surface_container_highest` fill with `primary` text. This feels integrated, not isolated.
*   **Tertiary:** No fill. `primary` text. High-contrast hover state using `primary_fixed` at 20% opacity.

### Quota Progress Bars
*   **Track:** `surface_container_high`.
*   **Indicator:** Linear gradient (`primary` to `primary_container`).
*   **Detail:** Use `label-sm` typography placed *above* the bar, never inside it, to maintain a clean visual line.

### Data Grids (Tables)
*   **Rule:** Forbid the use of vertical dividers.
*   **Style:** Zebra striping is prohibited. Use `body-md` for row text. 
*   **Status Indicators:** Use "Soft Chips." A background of the status color (e.g., `error_container`) with text in the `on_error_container` color. This ensures the status is visible but doesn't "vibrate" against the clean background.

### Input Fields
*   **Base:** `surface_container_low` fill, `none` border. 
*   **Focus State:** A 2px "Ghost Border" using `primary` at 40% opacity and a slight inner shadow to signify depth.

---

## 6. Do’s and Don’ts

### Do
*   **Do** use asymmetrical spacing. If a table is dense, give the header above it 48px of top padding to allow the eye to rest before diving into data.
*   **Do** use `surface_bright` for interactive "Hover" states on cards to create a subtle "glow" effect.
*   **Do** use the `tertiary` (Amber/Sienna) tones specifically for "Soft 404s" or "Warnings"—it provides a sophisticated alternative to "Alert Yellow."

### Don’t
*   **Don’t** use 100% opaque borders. They are the enemy of this system's "Clean Professional" aesthetic.
*   **Don’t** use shadows on cards that are part of the main page flow; reserve shadows only for temporary overlays (modals/menus).
*   **Don’t** use "pure" black (#000) or "pure" white (#fff) for everything. Always lean on the `surface` and `on_surface` tokens to maintain the Slate/Teal tonal harmony.