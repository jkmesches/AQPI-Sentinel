<script lang="ts">
	/** Full-screen mobile drill-down panel.
	 *
	 *  Spec calls for "proper return icon" and timeline/history-popup-like
	 *  content. Renders as a fixed cover that slides up; left-arrow header
	 *  closes. Uses safe-area-inset padding so it sits cleanly on notched
	 *  iPhones.
	 *
	 *  Generic enough to back both "tap a radar on the map" and "tap a
	 *  status row" — caller controls the body via the default snippet.
	 */
	import { stageLabel, stageTechCode, prettyCheckLabel } from '$lib/format';

	let {
		open = $bindable<boolean>(false),
		title = '',
		subtitle = '',
		stage = '',
		status = '',
		children
	}: {
		open: boolean;
		title?: string;
		subtitle?: string;
		stage?: string;
		status?: string;
		children?: any;
	} = $props();

	function close() { open = false; }

	function statusClass(s: string): string {
		return {
			pass:  'text-[var(--color-ok)]',
			warn:  'text-[var(--color-warn)]',
			fail:  'text-[var(--color-fail)]',
			error: 'text-[var(--color-fail)]',
			skip:  'text-[var(--color-muted)]'
		}[s] ?? 'text-[var(--color-muted)]';
	}

	// Capture-phase ESC handler so a popup-over-popup closes the
	// inner one without bubbling to the outer.
	function onKey(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			e.stopPropagation();
			close();
		}
	}
	$effect(() => {
		if (open) {
			document.addEventListener('keydown', onKey);
			// Lock body scroll while the sheet is up so the underlying
			// page doesn't move when the user scrolls within the drill-down.
			const prevOverflow = document.body.style.overflow;
			document.body.style.overflow = 'hidden';
			return () => {
				document.removeEventListener('keydown', onKey);
				document.body.style.overflow = prevOverflow;
			};
		}
	});
</script>

{#if open}
	<div
		class="fixed inset-0 z-[60] flex flex-col bg-[var(--color-canvas)]"
		role="dialog"
		aria-modal="true"
	>
		<!-- Sticky header with prominent back arrow. Padded for iOS notch. -->
		<header
			class="sticky top-0 z-10 flex items-center gap-3 border-b border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2.5"
			style="padding-top: max(env(safe-area-inset-top), 0.625rem);"
		>
			<button
				type="button"
				onclick={close}
				class="-ml-1 flex h-10 w-10 items-center justify-center rounded-full text-[var(--color-bright)] active:bg-[var(--color-elevated)]"
				aria-label="back"
				style="-webkit-tap-highlight-color: transparent;"
			>
				<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
					<polyline points="15 6 9 12 15 18" />
				</svg>
			</button>
			<div class="min-w-0 flex-1">
				<div class="flex items-center gap-2 text-[10px] uppercase tracking-[0.16em] text-[var(--color-muted)]">
					{#if stage}
						<span title={stage}>{stageLabel(stage)} ({stageTechCode(stage)})</span>
					{/if}
					{#if status}
						<span class="num {statusClass(status)}">{status}</span>
					{/if}
				</div>
				<div class="num truncate text-[15px] font-semibold text-[var(--color-bright)]">{title}</div>
				{#if subtitle}
					<div class="num truncate text-[11px] text-[var(--color-faint)]">{subtitle}</div>
				{/if}
			</div>
		</header>

		<!-- Body. Pads for iOS bottom safe area. -->
		<div
			class="flex-1 overflow-y-auto px-4 py-4"
			style="padding-bottom: max(env(safe-area-inset-bottom), 1rem);"
		>
			{@render children?.()}
		</div>
	</div>
{/if}
