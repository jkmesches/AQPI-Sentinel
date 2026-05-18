<script lang="ts">
	/** /m/push-settings — per-device push routing for the current user.
	 *
	 *  Lists this user's push subscriptions (one per device that subscribed
	 *  through Web Push). Tapping a device opens an editor where they can
	 *  set severity floor, product patterns, delay, and on-duty schedule.
	 *  Settings are saved per-subscription on the backend and consulted at
	 *  dispatch time by backend/push.py.
	 */
	import { onMount } from 'svelte';
	import PushRoutingEditor from '$lib/components/PushRoutingEditor.svelte';
	import MobileDrillDown from '$lib/components/mobile/MobileDrillDown.svelte';

	let subscriptions = $state<any[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	let selected = $state<any | null>(null);
	let editorOpen = $state(false);

	async function load() {
		loading = true;
		error = null;
		try {
			const r = await fetch('/api/push/subscriptions');
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			subscriptions = await r.json();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			loading = false;
		}
	}

	onMount(load);

	function open(s: any) {
		selected = s;
		editorOpen = true;
	}

	function deviceLabel(s: any): string {
		if (s.label) return s.label;
		const ua = s.user_agent ?? '';
		if (/iPhone/i.test(ua)) return 'iPhone';
		if (/iPad/i.test(ua))   return 'iPad';
		if (/Android/i.test(ua)) return 'Android';
		if (/Macintosh/i.test(ua)) return 'Mac';
		if (/Windows/i.test(ua)) return 'Windows';
		return 'Device';
	}

	function summary(s: any): string {
		const rc = s.routing_config ?? {};
		const bits: string[] = [];
		if (rc.severity_floor) bits.push(`${rc.severity_floor}+`);
		if ((rc.product_patterns ?? []).length) bits.push(`${rc.product_patterns.length} pattern${rc.product_patterns.length === 1 ? '' : 's'}`);
		if (rc.delay_s) bits.push(`${Math.round(rc.delay_s / 60)}m delay`);
		if (rc.schedule?.kind && rc.schedule.kind !== 'always') bits.push(rc.schedule.kind);
		return bits.length ? bits.join(' · ') : 'no filters — receives every notification';
	}
</script>

<div class="px-4 py-4"
	style="padding-top: max(env(safe-area-inset-top), 1rem); padding-bottom: calc(56px + env(safe-area-inset-bottom, 0));">
	<header class="mb-4">
		<h1 class="text-[18px] font-semibold tracking-wide text-[var(--color-bright)]">Push routing</h1>
		<div class="mt-1 text-[12px] text-[var(--color-muted)] leading-snug">
			Configure how each of your devices receives notifications. Filter by severity, match specific products, snooze with a delay, or set on-duty hours.
		</div>
	</header>

	{#if error}
		<div class="mb-3 rounded-md border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-3 py-2 text-[12px] text-[var(--color-fail)]">{error}</div>
	{/if}

	{#if loading}
		<div class="text-[12px] text-[var(--color-muted)]">loading…</div>
	{:else if subscriptions.length === 0}
		<div class="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-6 text-center text-[12px] text-[var(--color-muted)]">
			No subscribed devices. Enable notifications in <a href="/m/more" class="underline">More → Push notifications</a> first.
		</div>
	{:else}
		<ul class="overflow-hidden rounded-md border border-[var(--color-border)] bg-[var(--color-surface)]">
			{#each subscriptions as s}
				<li class="border-b border-[var(--color-border)]/60 last:border-b-0">
					<button
						type="button"
						onclick={() => open(s)}
						class="flex w-full items-start gap-3 px-3 py-3 text-left active:bg-[var(--color-elevated)]/60"
						style="-webkit-tap-highlight-color: transparent;"
					>
						<svg viewBox="0 0 24 24" width="22" height="22" class="mt-0.5 shrink-0 text-[var(--color-default)]" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
							<rect x="6" y="3" width="12" height="18" rx="2" />
							<line x1="11" y1="18" x2="13" y2="18" />
						</svg>
						<div class="min-w-0 flex-1">
							<div class="num text-[13.5px] font-medium text-[var(--color-bright)]">{deviceLabel(s)}</div>
							<div class="mt-0.5 text-[11px] text-[var(--color-muted)] num truncate">{summary(s)}</div>
							{#if s.user_agent}
								<div class="mt-0.5 text-[10.5px] text-[var(--color-faint)] num truncate">{s.user_agent}</div>
							{/if}
						</div>
						<svg viewBox="0 0 24 24" width="14" height="14" class="mt-2 shrink-0 text-[var(--color-faint)]" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
							<polyline points="9 6 15 12 9 18" />
						</svg>
					</button>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<MobileDrillDown
	bind:open={editorOpen}
	title={selected ? `Routing · ${selected.label || (selected.user_agent ?? '').slice(0, 32) || 'Device'}` : ''}
	subtitle="Per-device push filters"
>
	{#if selected}
		<PushRoutingEditor bind:subscription={selected} onSaved={load} />
	{/if}
</MobileDrillDown>
