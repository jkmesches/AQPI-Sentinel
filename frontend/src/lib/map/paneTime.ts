/**
 * What a pane publishes so the page can draw one time strip for both.
 *
 * The strip belongs below both panes rather than inside either. Rendered
 * inside the primary pane it takes height from that pane alone, so the two
 * maps end up different sizes and cover different ground at the same zoom.
 * A comparison between unequal areas is not a comparison, which is the same
 * fault as letting the panes drift in time.
 *
 * The primary pane still owns the state. It publishes this so the page can
 * place the control, and the page sends every interaction straight back.
 */

/** The time control's state and its handlers, as a pane publishes them. */
export interface TimeStrip {
	stepIdx: number;
	totalSteps: number;
	stepLabel: string;
	playing: boolean;
	/** Painted-pixel fraction per step, for the activity bars. */
	activity: readonly number[];
	/** Each step's timestamp, so the axis can place it by time. */
	times: readonly (string | null)[];
	/** Playback rate, one of playback.SPEEDS. */
	speed: number;
	onFirst: () => void;
	onPrev: () => void;
	onTogglePlay: () => void;
	onNext: () => void;
	onLast: () => void;
	onScrub: (i: number) => void;
	onSpeed: (s: number) => void;
}
