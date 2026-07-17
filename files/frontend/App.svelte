<script>
  import { ping } from "$python";

  let message = "Ready";

  async function checkBackend() {
    const result = await ping();
    message = result?.pong ? "Pong!" : JSON.stringify(result);
  }
</script>

<main>
  <h1>SyKit</h1>
  <button onclick={checkBackend}>Ping Python</button>
  <p>{message}</p>
</main>
