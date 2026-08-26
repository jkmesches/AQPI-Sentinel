<script lang="ts">
	/** /faq — in-app FAQ for the lab team.
	 *
	 * Content lives in $lib/faq.ts as data; this file is presentation only.
	 * The same question set is published on the docs site at
	 * docs/04-faq.md, and validation_tests/test_faq_parity.py fails if the
	 * two ever disagree about which questions exist.
	 */
	import { FAQ, FAQ_LIMITS, type FaqBlock } from '$lib/faq';

	// Minimal inline formatter: escape first, then allow **bold**, *italic*
	// and `code`. Content is authored by us in faq.ts, but escaping first
	// means a future edit can never turn into markup injection.
	function inline(text: string): string {
		const esc = text
			.replace(/&/g, '&amp;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;');
		return esc
			.replace(/`([^`]+)`/g, '<code class="num text-[var(--color-bright)] bg-[var(--color-elevated)] px-1 rounded-[2px]">$1</code>')
			.replace(/\*\*([^*]+)\*\*/g, '<strong class="text-[var(--color-bright)] font-semibold">$1</strong>')
			.replace(/\*([^*]+)\*/g, '<em>$1</em>');
	}

	let open = $state<Record<string, boolean>>(
		Object.fromEntries(FAQ.map((f) => [f.id, true]))
	);
	const allOpen = $derived(FAQ.every((f) => open[f.id]));

	function toggleAll() {
		const next = !allOpen;
		open = Object.fromEntries(FAQ.map((f) => [f.id, next]));
	}
</script>

<svelte:head><title>FAQ · AQPI Sentinel</title></svelte:head>

<!-- The app shell is `<main class="flex-1 overflow-hidden">`, so every page
     owns its own scroll region. Without this wrapper the FAQ was simply
     clipped at the viewport with no way to reach the later questions. -->
<div class="h-full overflow-y-auto">
<div class="mx-auto w-full max-w-[52rem] px-4 py-6">
	<header class="mb-5 border-b border-[var(--color-border)] pb-4">
		<div class="flex items-baseline justify-between gap-3">
			<h1 class="text-[18px] font-semibold tracking-[0.02em] text-[var(--color-bright)]">
				Frequently asked questions
			</h1>
			<button
				type="button"
				class="shrink-0 border border-[var(--color-border-strong)] px-2 py-0.5 text-[10.5px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)] hover:bg-[var(--color-elevated)]"
				onclick={toggleAll}
			>{allOpen ? 'collapse all' : 'expand all'}</button>
		</div>
		<p class="mt-2 text-[12.5px] leading-relaxed text-[var(--color-default)]">
			For people who use Sentinel's readings rather than its code — what it watches,
			where the numbers come from, and how it decides something is wrong.
		</p>
		<p class="mt-2 text-[12px] leading-relaxed text-[var(--color-muted)]">
			Short version: Sentinel independently re-derives the health of
			<span class="num">radarca.engr.colostate.edu</span> from that site's own public
			APIs, every couple of minutes, and keeps the evidence.
		</p>
	</header>

	<!-- Jump list: on a dashboard people arrive looking for one answer. -->
	<nav class="mb-6 flex flex-wrap gap-x-3 gap-y-1.5 text-[11px]">
		{#each FAQ as f, i}
			<a
				href="#{f.id}"
				class="text-[var(--color-muted)] hover:text-[var(--color-accent)] transition-colors"
			>{i + 1}. {f.q.replace(/\?$/, '')}</a>
		{/each}
	</nav>

	{#each FAQ as f, i}
		<section id={f.id} class="mb-4 scroll-mt-4 border border-[var(--color-border)] bg-[var(--color-canvas)]/40">
			<button
				type="button"
				class="flex w-full items-start gap-2.5 px-3.5 py-2.5 text-left hover:bg-[var(--color-elevated)]/50 transition-colors"
				onclick={() => (open[f.id] = !open[f.id])}
				aria-expanded={open[f.id]}
			>
				<span class="num shrink-0 text-[11px] text-[var(--color-faint)] mt-[3px]">{i + 1}</span>
				<span class="flex-1 text-[13.5px] font-medium text-[var(--color-bright)]">{f.q}</span>
				<span class="shrink-0 text-[var(--color-faint)] text-[12px] mt-[2px]">{open[f.id] ? '−' : '+'}</span>
			</button>

			{#if open[f.id]}
				<div class="border-t border-[var(--color-border)] px-3.5 py-3">
					{#each f.blocks as b (b)}
						{@render block(b)}
					{/each}
				</div>
			{/if}
		</section>
	{/each}

	<section class="mb-6 border border-[var(--color-warn)]/30 bg-[var(--color-warn)]/[0.04] px-3.5 py-3">
		<h2 class="mb-2 text-[13px] font-semibold text-[var(--color-bright)]">
			What Sentinel does not do
		</h2>
		<p class="mb-2 text-[12px] text-[var(--color-muted)]">
			Worth being explicit, so the readings aren't over-read.
		</p>
		<ul class="space-y-1.5">
			{#each FAQ_LIMITS as l}
				<li class="flex gap-2 text-[12.5px] leading-relaxed text-[var(--color-default)]">
					<span class="text-[var(--color-faint)]">·</span>
					<span>{@html inline(l)}</span>
				</li>
			{/each}
		</ul>
	</section>

	<footer class="border-t border-[var(--color-border)] pt-3 text-[11px] text-[var(--color-muted)]">
		A longer version of this page, plus deployment and maintenance guides, lives on the
		<a
			class="text-[var(--color-accent)] hover:text-[var(--color-bright)] transition-colors"
			href="https://jkmesches.github.io/SentinelProject/04-faq/"
			target="_blank"
			rel="noopener"
		>documentation site</a>.
	</footer>
</div>
</div>

{#snippet block(b: FaqBlock)}
	{#if b.kind === 'p'}
		<p class="mb-2.5 text-[12.5px] leading-relaxed text-[var(--color-default)]">{@html inline(b.text)}</p>
	{:else if b.kind === 'note'}
		<div class="mb-2.5 border-l-2 border-[var(--color-accent)]/50 bg-[var(--color-elevated)]/40 px-3 py-2 text-[12.5px] leading-relaxed text-[var(--color-default)]">
			{@html inline(b.text)}
		</div>
	{:else if b.kind === 'table'}
		<div class="mb-2.5 overflow-x-auto">
			<table class="w-full border-collapse text-[12px]">
				<thead>
					<tr class="border-b border-[var(--color-border-strong)]">
						{#each b.head as h}
							<th class="px-2 py-1 text-left font-semibold uppercase tracking-wider text-[10.5px] text-[var(--color-muted)]">{h}</th>
						{/each}
					</tr>
				</thead>
				<tbody>
					{#each b.rows as row}
						<tr class="border-b border-[var(--color-border)]">
							{#each row as cell}
								<td class="px-2 py-1 align-top text-[var(--color-default)]">{@html inline(cell)}</td>
							{/each}
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
{/snippet}
