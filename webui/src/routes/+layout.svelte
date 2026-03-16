<script lang="ts">
  import '../app.css';
  import { onMount } from 'svelte';
  import type { Snippet } from 'svelte';
  import { getVapidKey, pushSubscribe } from '$lib/api';

  let { children }: { children: Snippet } = $props();

  async function setupPush(registration: ServiceWorkerRegistration) {
    if (!('PushManager' in window)) return;

    // Check if already subscribed
    const existing = await registration.pushManager.getSubscription();
    if (existing) {
      await pushSubscribe(existing);
      return;
    }

    // Request permission
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') return;

    // Get VAPID key and subscribe
    try {
      const vapidKey = await getVapidKey();
      const key = Uint8Array.from(atob(vapidKey.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: key
      });
      await pushSubscribe(subscription);
    } catch (e) {
      console.warn('Push subscription failed:', e);
    }
  }

  onMount(() => {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').then((reg) => {
        setupPush(reg);
      }).catch(() => {
        // SW registration failed — app still works without it
      });
    }
  });
</script>

{@render children()}
