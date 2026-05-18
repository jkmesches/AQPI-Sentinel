<script lang="ts">
	/** /settings/devices — desktop mirror of /m/push-settings.
	 *
	 *  Same data + same editor; rendered side-by-side instead of full-screen
	 *  drill-down. Lets users who set up notifications on their phone but
	 *  configure on a laptop edit without leaving their desk.
	 */
	import { onMount } from 'svelte';
	import PushRoutingEditor from '$lib/components/PushRoutingEditor.svelte';

	let subscriptions = $state<any[]>([]);
	let selected = $state<any | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	async function load() {
		loading = true;
		error = null;
		try {
			const r = await fetch('/api/push/subscriptions');
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			subscriptions = await r.json();
			if (subscriptions.length && !selected) {
				selected = subscriptions[0];
			}
		} catch (e) {
			error = (e as Error).message;
		} finally {
			loading = false;
		}
	}
	onMount(load);

	function deviceLabel(s: any): string {
		if (s?.label) return s.label;
		const ua = s?.user_agent ?? '';
		if (/iPhone/i.test(ua)) return 'iPhone';
		if (/iPad/i.test(ua))   return 'iPad';
		if (/Android/i.test(ua)) return 'Android';
		if (/Macintosh/i.test(ua)) return 'Mac';
		if (/Windows/i.test(ua)) return 'Windows';
		return 'Device';
	}
</script>

<div class="flex h-full">
	<!-- Device list -->
	<aside class="w-64 shrink-0 overflow-y-auto border-r border-[var(--color-border)] bg-[var(--color-canvas)]/40">
		<header class="px-4 py-3 border-b border-[var(--color-border)]">
			<div class="text-[10px] uppercase tracking-[0.18em] text-[var(--color-muted)]">Push routing</div>
			<div class="text-[12px] text-[var(--color-default)] mt-0.5">Your subscribed devices</div>
		</header>
		{#if loading}
			<div class="px-4 py-3 text-[12px] text-[var(--color-muted)]">loading…</div>
		{:else if subscriptions.length === 0}
			<div class="px-4 py-3 text-[12px] text-[var(--color-faint)] italic">
				No subscribed devices. Enable Web Push on your mobile device first (open Sentinel on iPhone Safari → Add to Home Screen → enable notifications).
			</div>
		{:else}
			<ul>
				{#each subscriptions as s}
					<li>
						<button
							class="flex w-full items-start gap-2 border-b border-[var(--color-border)]/40 px-3 py-2 text-left text-[12px] {selected?.id === s.id ? 'bg-[var(--color-elevated)]' : 'hover:bg-[var(--color-elevated)]/40'}"
							onclick={() => (selected = s)}
						>
							<div class="min-w-0 flex-1">
								<div class="num font-medium text-[var(--color-bright)]">{deviceLabel(s)}</div>
								<div class="text-[10px] text-[var(--color-faint)] truncate">{s.user_agent ?? ''}</div>
							</div>
						</button>
					</li>
				{/each}
			</ul>
		{/if}
	</aside>

	<!-- Editor -->
	<main class="flex-1 overflow-y-auto px-6 py-5">
		{#if error}
			<div class="mb-3 border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-3 py-2 text-[12px] text-[var(--color-fail)]">{error}</div>
		{/if}
		{#if selected}
			<header class="mb-4">
				<h1 class="text-[15px] font-semibold tracking-wide text-[var(--color-bright)]">{deviceLabel(selected)}</h1>
				<div class="text-[11px] text-[var(--color-muted)] mt-0.5">{selected.user_agent ?? ''}</div>
			</header>
			<div class="max-w-2xl">
				<PushRoutingEditor bind:subscription={selected} onSaved={load} />
			</div>
		{:else if !loading}
			<div class="text-[12px] text-[var(--color-muted)] italic">No device selected.</div>
		{/if}
	</main>
</div>
