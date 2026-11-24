/**
 * The theme's colors for code that cannot read CSS custom properties.
 *
 * MapLibre paint expressions take literal colors, so the map's markers,
 * labels and halos read these instead of the --color-* tokens. Each value is
 * the token of the same name in app.css (dark from @theme, light from
 * [data-theme="light"]), and validation_tests/test_theme_palette.py fails if
 * the two drift apart.
 */

export type PaletteTheme = 'light' | 'dark';

/** The tokens the map paints with, by their app.css names. */
export interface Palette {
	canvas: string;
	default: string;
	accent: string;
	ok: string;
	warn: string;
	fail: string;
	error: string;
	suppress: string;
}

export const PALETTE: Record<PaletteTheme, Palette> = {
	dark: {
		canvas: '#0a130d',
		default: '#e3e8e1',
		accent: '#C8C372',
		ok: '#39a758',
		warn: '#ffc038',
		fail: '#f76e6e',
		error: '#976ef7',
		suppress: '#8f978e'
	},
	light: {
		canvas: '#f6f6f2',
		default: '#1a1d1a',
		accent: '#1E4D2B',
		ok: '#1f7a45',
		warn: '#7a5000',
		fail: '#7a1426',
		error: '#4b2a9e',
		suppress: '#59595b'
	}
};

/**
 * The marker color for each check verdict, in one theme.
 *
 * An error draws in the fail color on the map, as it always has: a radar pin
 * has room for one dot, and a check that could not reach its radar leaves the
 * operator with the same question as one that found it down.
 *
 * @param theme The resolved theme.
 * @returns Verdict name to color, with skip for anything unknown.
 */
export function verdictColors(theme: PaletteTheme): Record<string, string> {
	const p = PALETTE[theme];
	return { pass: p.ok, warn: p.warn, fail: p.fail, error: p.fail, skip: p.suppress };
}
