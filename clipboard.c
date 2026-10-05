/* $OpenBSD$ */

/*
 * Copyright (c) 2026 tmux contributors
 *
 * Permission to use, copy, modify, and distribute this software for any
 * purpose with or without fee is hereby granted, provided that the above
 * copyright notice and this permission notice appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
 * WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
 * ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
 * WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION
 * OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
 * CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
 */

#include <sys/types.h>

#include <ctype.h>
#include <event.h>
#include <resolv.h>
#include <stdlib.h>
#include <string.h>

#include "tmux.h"

/*
 * OSC 5522 is a routed protocol, not display output. In particular, do not
 * decode/assemble clipboard data here, apply paste-buffer limits, log it, or
 * send it through the lossy display-output queue policy.
 */
#define CLIPBOARD_FRAME_LIMIT INPUT_BUF_DEFAULT_SIZE
#define CLIPBOARD_WATERMARK (256 * 1024)
#define CLIPBOARD_TIMEOUT 30000
#define CLIPBOARD_NO_PANE UINT_MAX

struct clipboard_packet {
	char	*storage;
	char	*type;
	char	*status;
	char	*id;
	char	*pw;
	char	*name;
	char	*loc;
	char	*mime;
	const char *body;
};

struct clipboard_output {
	struct clipboard_output *next;
	size_t			 start, end;
	int			 started;
};

struct clipboard_wait {
	struct cmdq_item	*item;
	u_int		 count;
	int		 failed;
};

struct clipboard {
	struct tty	*tty;
	struct event	 timer;
	struct event	 resume;
	int		 input_paused;
	int		 supported;
	int		 probing;
	u_int		 queries[128];
	size_t		 nqueries;
	uint64_t	 probe_time;
	u_int		 owner;
	u_int		 session;
	int		 enabled;
	uint64_t	 generation;
	uint64_t	 frame_generation;
	int		 offer;
	char		*grant;
	char		*location;
	char		*revoked[64];
	size_t		 nrevoked, revoked_bytes;
	int		 deny_unknown_grants;
	uint64_t	 grant_time;
	u_int		 request_pane;
	int		 writing;
	char		*original_id;
	char		 mapped_id[64];
	uint64_t	 request_time;
	int		 read_started;
	int		 granted;
	u_int		 output_pane;
	char		*frame;
	size_t		 frame_len;
	int		 framing;
	int		 report;
	int		 discard;
	int		 escape;
	struct clipboard_output *output;
	int		 closing;
	int		 suspending;
	char		*exec_command;
	int		 quarantined;
	uint64_t	 handoff_time;
	struct clipboard_wait *wait;
	int		 opted_in;
	int		 verified;
	char		 fence_id[64];
	int		 fence_stage;
};

static void clipboard_tick(int, short, void *);

static int
clipboard_drained(struct clipboard *cb)
{
	return (EVBUFFER_LENGTH(cb->tty->out) == 0 &&
	    !cb->framing && (!cb->verified || cb->fence_stage == 3));
}

static void
clipboard_wait_done(struct clipboard *cb, const char *error)
{
	struct clipboard_wait *wait = cb->wait;

	if (wait == NULL)
		return;
	cb->wait = NULL;
	if (error != NULL) {
		cmdq_error(wait->item, "client %s: %s",
		    cb->tty->client->name, error);
		wait->failed = 1;
	}
	if (--wait->count == 0) {
		cmdq_continue(wait->item);
		free(wait);
	}
}

struct clipboard_wait *
clipboard_wait_begin(struct cmdq_item *item)
{
	struct clipboard_wait *wait = xcalloc(1, sizeof *wait);

	wait->item = item;
	return (wait);
}

void
clipboard_wait_add(struct clipboard_wait *wait, struct client *c)
{
	struct clipboard *cb = c->tty.clipboard;

	if (cb == NULL || !cb->closing)
		return;
	if (!cb->quarantined && !cb->suspending && clipboard_drained(cb))
		return;
	if (cb->quarantined || cb->wait != NULL) {
		cmdq_error(wait->item, "client %s: clipboard handoff %s",
		    c->name, cb->quarantined ? "failed; attachment quarantined" :
		    "already pending");
		wait->failed = 1;
		return;
	}
	cb->wait = wait;
	wait->count++;
}

enum cmd_retval
clipboard_wait_end(struct clipboard_wait *wait)
{
	enum cmd_retval result;

	if (wait->count != 0)
		return (CMD_RETURN_WAIT);
	result = wait->failed ? CMD_RETURN_ERROR : CMD_RETURN_NORMAL;
	free(wait);
	return (result);
}

const char *
clipboard_state(struct tty *tty)
{
	struct clipboard *cb = tty->clipboard;

	if (cb == NULL)
		return ("inactive");
	if (cb->quarantined)
		return (clipboard_drained(cb) ? "quarantined-ready" : "quarantined");
	if (cb->closing)
		return ("draining");
	return ("normal");
}

void
clipboard_handoff_done(struct tty *tty)
{
	if (tty->clipboard != NULL) {
		clipboard_wait_done(tty->clipboard, NULL);
		tty->clipboard->suspending = 0;
	}
}

static void
clipboard_finish_handoff(struct clipboard *cb)
{
	struct timeval tv = { 0, 0 };

	if (cb->closing && !cb->quarantined && cb->suspending &&
	    clipboard_drained(cb))
		evtimer_add(&cb->timer, &tv);
}

static void
clipboard_resume(__unused int fd, __unused short events, void *data)
{
	struct clipboard *cb = data;

	while (tty_keys_next(cb->tty))
		;
	clipboard_finish_handoff(cb);
}

static void
clipboard_resume_input(struct clipboard *cb)
{
	struct timeval tv = { 0, 0 };

	if (!cb->input_paused || cb->closing)
		return;
	cb->input_paused = 0;
	event_add(&cb->tty->event_in, NULL);
	evtimer_add(&cb->resume, &tv);
}

static void
clipboard_arm(struct clipboard *cb)
{
	uint64_t	now = get_timer(), deadline = UINT64_MAX, candidate;
	struct timeval	tv;

	if (cb->closing)
		return;
	if (cb->probing)
		deadline = cb->probe_time + 1000;
	if (cb->grant != NULL || cb->offer) {
		candidate = cb->grant_time + CLIPBOARD_TIMEOUT;
		if (candidate < deadline)
			deadline = candidate;
	}
	if (cb->request_pane != CLIPBOARD_NO_PANE) {
		candidate = cb->request_time + CLIPBOARD_TIMEOUT;
		if (candidate < deadline)
			deadline = candidate;
	}
	evtimer_del(&cb->timer);
	if (deadline == UINT64_MAX)
		return;
	if (deadline <= now)
		deadline = now + 1;
	tv.tv_sec = (deadline - now) / 1000;
	tv.tv_usec = ((deadline - now) % 1000) * 1000;
	evtimer_add(&cb->timer, &tv);
}

static struct window_pane *
clipboard_pane(struct clipboard *cb)
{
	struct client		*c = cb->tty->client;
	struct window_pane	*wp;

	if (!(cb->tty->flags & TTY_STARTED) || c->session == NULL ||
	    cb->closing ||
	    (c->flags & (CLIENT_UNATTACHEDFLAGS|CLIENT_READONLY)) ||
	    !(c->flags & CLIENT_FOCUSED) ||
	    c->prompt != NULL || c->overlay_draw != NULL)
		return (NULL);
	wp = server_client_get_pane(c);
	if (wp == NULL || wp->event == NULL || wp->screen != &wp->base ||
	    window_pane_has_prompt(wp))
		return (NULL);
	return (wp);
}

static void
clipboard_send(struct clipboard *cb, const char *data, size_t size)
{
	struct tty	*tty = cb->tty;
	struct clipboard_output *output, **tail;

	/* Never let tty_block_maybe discard protocol bytes already queued. */
	output = xcalloc(1, sizeof *output);
	output->start = EVBUFFER_LENGTH(tty->out);
	output->end = output->start + size;
	for (tail = &cb->output; *tail != NULL; tail = &(*tail)->next)
		;
	*tail = output;
	tty->flags |= TTY_NOBLOCK;
	evbuffer_add(tty->out, data, size);
	tty->client->written += size;
	if (tty->flags & TTY_STARTED)
		event_add(&tty->event_out, NULL);
}

void
clipboard_written(struct tty *tty, size_t size)
{
	struct clipboard	*cb = tty->clipboard;
	struct clipboard_output *output, **at;

	if (cb == NULL)
		return;
	for (at = &cb->output; (output = *at) != NULL;) {
		if (size >= output->end) {
			*at = output->next;
			free(output);
			continue;
		}
		if (size > output->start) {
			output->started = 1;
			output->start = 0;
		} else
			output->start -= size;
		output->end -= size;
		at = &output->next;
	}
	clipboard_finish_handoff(cb);
}

/*
 * Preserve the remainder of a started OSC, but not subsequent packets. Off is
 * queued behind that complete frame, never injected by a synchronous raw write.
 */
static void
clipboard_cancel_output(struct clipboard *cb)
{
	struct clipboard_output *output, *next;
	size_t			 keep;
	char			*data;

	if (cb->output != NULL) {
		output = cb->output;
		keep = output->started ? output->end : output->start;
		data = xmalloc(keep == 0 ? 1 : keep);
		memcpy(data, EVBUFFER_DATA(cb->tty->out), keep);
		evbuffer_drain(cb->tty->out, EVBUFFER_LENGTH(cb->tty->out));
		evbuffer_add(cb->tty->out, data, keep);
		free(data);
		next = output->next;
		if (output->started) {
			output->next = NULL;
			output = next;
		} else
			cb->output = NULL;
		while (output != NULL) {
			next = output->next;
			free(output);
			output = next;
		}
		cb->tty->client->flags |= CLIENT_ALLREDRAWFLAGS;
	}
	clipboard_send(cb, "\033[?5522l", 8);
}

static void
clipboard_reply(struct window_pane *wp, const char *type, const char *id,
    const char *status)
{
	char	*reply;

	if (wp == NULL || wp->event == NULL)
		return;
	xasprintf(&reply, "\033]5522;type=%s%s%s:status=%s\033\\", type,
	    id != NULL && *id != '\0' ? ":id=" : "", id != NULL ? id : "",
	    status);
	bufferevent_write(wp->event, reply, strlen(reply));
	free(reply);
}

static void
clipboard_clear_request(struct clipboard *cb)
{
	cb->request_pane = CLIPBOARD_NO_PANE;
	cb->writing = cb->read_started = cb->granted = 0;
	free(cb->original_id);
	cb->original_id = NULL;
	*cb->mapped_id = '\0';
	clipboard_resume_input(cb);
}

static void
clipboard_clear_grant(struct clipboard *cb)
{
	if (cb->grant != NULL) {
		if (cb->nrevoked < nitems(cb->revoked) &&
		    cb->revoked_bytes + strlen(cb->grant) <= 65536) {
			cb->revoked[cb->nrevoked++] = cb->grant;
			cb->revoked_bytes += strlen(cb->grant);
		} else {
			/* Never reclassify forgotten authority as an ambient token. */
			cb->deny_unknown_grants = 1;
			free(cb->grant);
		}
	}
	free(cb->location);
	cb->grant = cb->location = NULL;
	cb->offer = 0;
}

/*
 * Do not silently skip malformed write packets and later commit a valid
 * prefix. Poison the outer transaction with invalid base64 (specified EINVAL),
 * then forget its mapping and drain the pane's continuations.
 */
void
clipboard_invalid_write(struct window_pane *wp)
{
	struct client		*c;
	struct clipboard	*cb;
	char			*abort;

	if (wp == NULL)
		return;
	TAILQ_FOREACH(c, &clients, entry) {
		cb = c->tty.clipboard;
		if (cb == NULL || !cb->writing || cb->request_pane != wp->id)
			continue;
		xasprintf(&abort, "\033]5522;type=wdata:id=%s:"
		    "mime=YXBwbGljYXRpb24vb2N0ZXQtc3RyZWFt;!\033\\",
		    cb->mapped_id);
		clipboard_send(cb, abort, strlen(abort));
		free(abort);
		clipboard_reply(wp, "write", cb->original_id, "EINVAL");
		clipboard_clear_request(cb);
	}
}

static int
clipboard_opted_in(struct client *c)
{
	if (c->flags & CLIENT_NO_CLIPBOARD_FENCE)
		return (0);
	return ((c->flags & CLIENT_CLIPBOARD_FENCE) ||
	    options_get_number(global_options, "native-clipboard"));
}

void
clipboard_sync(struct tty *tty)
{
	struct clipboard	*cb = tty->clipboard;
	struct window_pane	*wp;
	u_int			 owner, session;
	int			 enabled, opted_in;

	/* A handoff owns its generation until completion, even if policy changes. */
	if (cb == NULL || cb->closing)
		return;
	wp = clipboard_pane(cb);
	owner = wp != NULL ? wp->id : CLIPBOARD_NO_PANE;
	session = tty->client->session != NULL ?
	    tty->client->session->id : UINT_MAX;
	opted_in = clipboard_opted_in(tty->client);
	if (opted_in && cb->supported)
		cb->verified = 1; /* Cleanup obligations survive disabling. */
	enabled = opted_in && cb->supported && wp != NULL &&
	    (wp->base.mode & MODE_CLIPBOARD);
	if (owner == cb->owner && session == cb->session &&
	    enabled == cb->enabled && opted_in == cb->opted_in)
		return;

	/* Off/on is intentional even between two enabled panes. */
	cb->generation++;
	if (cb->enabled || cb->request_pane != CLIPBOARD_NO_PANE ||
	    cb->grant != NULL || cb->offer)
		clipboard_cancel_output(cb);
	clipboard_clear_grant(cb);
	clipboard_clear_request(cb);
	cb->owner = owner;
	cb->session = session;
	cb->opted_in = opted_in;
	cb->enabled = enabled;
	if (enabled)
		clipboard_send(cb, "\033[?5522h", 8);
	clipboard_arm(cb);
}

static void
clipboard_queries(struct clipboard *cb)
{
	struct window_pane	*wp;
	char			 reply[32];
	size_t			 i;
	int			 state;

	for (i = 0; i < cb->nqueries; i++) {
		wp = window_pane_find_by_id(cb->queries[i]);
		if (wp == NULL || wp->event == NULL)
			continue;
		state = cb->supported && clipboard_opted_in(cb->tty->client) ?
		    ((wp->base.mode & MODE_CLIPBOARD) ? 1 : 2) : 0;
		xsnprintf(reply, sizeof reply, "\033[?5522;%d$y", state);
		bufferevent_write(wp->event, reply, strlen(reply));
	}
	cb->nqueries = 0;
}

void
clipboard_start(struct tty *tty)
{
	struct clipboard	*cb;

	if (tty->clipboard != NULL)
		clipboard_stop(tty);
	cb = tty->clipboard = xcalloc(1, sizeof *cb);
	cb->tty = tty;
	cb->owner = cb->request_pane = cb->output_pane = CLIPBOARD_NO_PANE;
	cb->session = UINT_MAX;
	cb->frame = xmalloc(CLIPBOARD_FRAME_LIMIT + 1);
	cb->framing = tty->clipboard_discard;
	cb->report = tty->clipboard_report;
	cb->discard = tty->clipboard_discard;
	cb->escape = tty->clipboard_escape;
	evtimer_set(&cb->timer, clipboard_tick, cb);
	evtimer_set(&cb->resume, clipboard_resume, cb);
	if (tty->term->flags & TERM_VT100LIKE) {
		cb->probing = 1;
		cb->probe_time = get_timer();
		/*
		 * Revoke authority left by a previous attachment, including an
		 * unclean disconnect. ST resynchronizes an abandoned outer OSC;
		 * no transaction is admitted until the subsequent probe replies.
		 */
		clipboard_send(cb, "\033\\\033[?5522l", 10);
		clipboard_send(cb, "\033[?5522$p", 10);
	}
	clipboard_arm(cb);
}

void
clipboard_stop(struct tty *tty)
{
	struct clipboard	*cb = tty->clipboard;
	struct clipboard_output *output, *next;
	size_t			 i;

	if (cb == NULL)
		return;
	cb->closing = 1;
	clipboard_wait_done(cb, "connection closed during clipboard handoff");
	evtimer_del(&cb->timer);
	evtimer_del(&cb->resume);
	tty->clipboard_discard = cb->framing;
	tty->clipboard_report = cb->report;
	tty->clipboard_escape = cb->escape;
	cb->supported = 0;
	clipboard_queries(cb);
	clipboard_clear_grant(cb);
	for (i = 0; i < cb->nrevoked; i++)
		free(cb->revoked[i]);
	clipboard_clear_request(cb);
	free(cb->frame);
	free(cb->exec_command);
	for (output = cb->output; output != NULL; output = next) {
		next = output->next;
		free(output);
	}
	free(cb);
	tty->clipboard = NULL;
}

/* Defer orderly detach/suspend until the active OSC and cancellation finish. */
int
clipboard_drain(struct tty *tty, int suspending)
{
	struct clipboard	*cb = tty->clipboard;
	struct timeval		 tv = { 0, 0 };
	uint32_t		 nonce[4];
	char			*request;

	if (cb == NULL)
		return (1);
	if (!cb->verified && !cb->framing && cb->output == NULL &&
	    !cb->closing)
		return (1); /* Preserve unsupported-terminal legacy handoff. */
	if (cb->quarantined) {
		if (!clipboard_drained(cb))
			return (0);
		/* Only this explicit new handoff request may leave quarantine. */
		cb->quarantined = 0;
		cb->suspending = suspending;
		cb->handoff_time = get_timer();
	}
	if (!cb->closing) {
		cb->closing = 1;
		cb->suspending = suspending;
		cb->handoff_time = get_timer();
		cb->generation++;
		if (cb->verified) {
			/* Register before emitting any packet in this cut. */
			arc4random_buf(nonce, sizeof nonce);
			xsnprintf(cb->fence_id, sizeof cb->fence_id,
			    "tmux-fence-%08x%08x%08x%08x",
			    nonce[0], nonce[1], nonce[2], nonce[3]);
			cb->fence_stage = 0;
		}
		clipboard_cancel_output(cb);
		if (cb->verified) {
			xasprintf(&request, "\033]5522;type=read:id=%s;Lg==\033\\",
			    cb->fence_id);
			clipboard_send(cb, request, strlen(request));
			free(request);
		}
		clipboard_clear_grant(cb);
		clipboard_clear_request(cb);
		cb->enabled = 0;
		/* Finish/discard the frame before any external TTY reader owns it. */
		cb->discard = 1;
		cb->input_paused = 0;
		event_add(&tty->event_in, NULL);
		evtimer_add(&cb->resume, &tv);
		tv.tv_sec = 2;
		evtimer_add(&cb->timer, &tv);
	}
	return (clipboard_drained(cb));
}

int
clipboard_closing(struct tty *tty)
{
	return (tty->clipboard != NULL && tty->clipboard->closing);
}

int
clipboard_defer_exec(struct tty *tty, const char *command)
{
	struct clipboard *cb = tty->clipboard;

	if (cb == NULL)
		return (1);
	if (!cb->closing || cb->quarantined) {
		free(cb->exec_command);
		cb->exec_command = xstrdup(command);
	}
	return (clipboard_drain(tty, 3));
}

int
clipboard_partial_output(struct tty *tty)
{
	struct clipboard *cb = tty->clipboard;

	return (cb != NULL && cb->output != NULL && cb->output->started);
}

static void
clipboard_tick(__unused int fd, __unused short events, void *data)
{
	struct clipboard	*cb = data;
	uint64_t		 now = get_timer();

	if (cb->closing) {
		if (cb->quarantined)
			return;
		if (cb->suspending && clipboard_drained(cb)) {
			if (cb->tty->client->flags & CLIENT_EXIT)
				return;
			clipboard_wait_done(cb, NULL);
			if (cb->suspending == 1)
				server_client_suspend(cb->tty->client);
			else if (cb->suspending == 2)
				server_lock_client(cb->tty->client);
			else {
				cb->suspending = 0;
				server_client_exec(cb->tty->client, cb->exec_command);
			}
			return; /* stop/lock may have freed cb. */
		}
		if (!clipboard_drained(cb) && now - cb->handoff_time >= 2000) {
			cb->quarantined = 1;
			cb->suspending = 0;
			if (cb->tty->client->exit_type == CLIENT_EXIT_DETACH) {
				cb->tty->client->flags &= ~CLIENT_EXIT;
				free(cb->tty->client->exit_session);
				cb->tty->client->exit_session = NULL;
			}
			clipboard_wait_done(cb,
			    "clipboard handoff failed after 2s; attachment "
			    "quarantined (NOT locked); retry only after boundary "
			    "recovery or close terminal and reconnect");
			control_notify_clipboard_handoff_failed(cb->tty->client);
			server_add_message("client %s clipboard handoff failed; "
			    "quarantined, NOT locked", cb->tty->client->name);
		}
		return;
	}
	clipboard_sync(cb->tty);
	if (cb->probing && now - cb->probe_time >= 1000) {
		cb->probing = 0;
		clipboard_queries(cb);
	}
	if ((cb->grant != NULL || cb->offer) &&
	    now - cb->grant_time >= CLIPBOARD_TIMEOUT)
		clipboard_clear_grant(cb);
	if (cb->request_pane != CLIPBOARD_NO_PANE &&
	    now - cb->request_time >= CLIPBOARD_TIMEOUT) {
		if (cb->writing)
			clipboard_reply(window_pane_find_by_id(cb->request_pane),
			    "write", cb->original_id, "EIO");
		clipboard_cancel_output(cb);
		if (cb->enabled)
			clipboard_send(cb, "\033[?5522h", 8);
		clipboard_clear_request(cb);
	}
	clipboard_arm(cb);
}

void
clipboard_pane_drained(struct window_pane *wp)
{
	struct client		*c;
	struct clipboard	*cb;

	TAILQ_FOREACH(c, &clients, entry) {
		cb = c->tty.clipboard;
		if (cb == NULL || cb->closing || cb->request_pane != wp->id)
			continue;
		if (EVBUFFER_LENGTH(wp->event->output) >= CLIPBOARD_WATERMARK)
			continue;
		clipboard_resume_input(cb);
	}
}

/*
 * Metadata validation is structural. Clipboard content, base64 write chunk
 * boundaries and permission decisions belong to the terminal, not the mux.
 */
static int
clipboard_parse(const char *body, struct clipboard_packet *p)
{
	char	*meta, *field, *equal, **slot;
	size_t	 i;

	memset(p, 0, sizeof *p);
	p->body = body;
	p->storage = xstrdup(body);
	meta = p->storage;
	field = strchr(meta, ';');
	if (field != NULL)
		*field = '\0';
	while ((field = strsep(&meta, ":")) != NULL) {
		equal = strchr(field, '=');
		if (equal == NULL || equal == field)
			return (0);
		*equal++ = '\0';
		slot = NULL;
		if (strcmp(field, "type") == 0)
			slot = &p->type;
		else if (strcmp(field, "status") == 0)
			slot = &p->status;
		else if (strcmp(field, "id") == 0)
			slot = &p->id;
		else if (strcmp(field, "pw") == 0)
			slot = &p->pw;
		else if (strcmp(field, "name") == 0)
			slot = &p->name;
		else if (strcmp(field, "loc") == 0)
			slot = &p->loc;
		else if (strcmp(field, "mime") == 0)
			slot = &p->mime;
		if (slot != NULL) {
			if (*slot != NULL)
				return (0);
			*slot = equal;
		}
	}
	/* No controls, which could escape the OSC when forwarded. */
	for (i = 0; body[i] != '\0'; i++) {
		if ((u_char)body[i] < 0x20 || (u_char)body[i] > 0x7e)
			return (0);
	}
	if (p->id != NULL) {
		/* The protocol requires removal of unsafe ID characters. */
		equal = p->id;
		for (field = p->id; *field != '\0'; field++) {
			if (isalnum((u_char)*field) ||
			    strchr("-_+.", *field) != NULL)
				*equal++ = *field;
		}
		*equal = '\0';
	}
	return (p->type != NULL);
}

/* Rebuild only id; all other metadata and the payload are untouched. */
static char *
clipboard_map(const char *body, const char *id)
{
	struct evbuffer	*buffer = evbuffer_new();
	const char	*p = body, *end, *payload = strchr(body, ';');
	char		*result;
	size_t		 len;
	int		 first = 1;

	if (buffer == NULL)
		fatalx("out of memory");
	if (payload == NULL)
		payload = body + strlen(body);
	evbuffer_add(buffer, "\033]5522;", 7);
	while (p < payload) {
		end = memchr(p, ':', payload - p);
		if (end == NULL)
			end = payload;
		if (end - p < 3 || strncmp(p, "id=", 3) != 0) {
			if (!first)
				evbuffer_add(buffer, ":", 1);
			evbuffer_add(buffer, p, end - p);
			first = 0;
		}
		p = end + 1;
	}
	if (id != NULL && *id != '\0') {
		evbuffer_add(buffer, ":id=", 4);
		evbuffer_add(buffer, id, strlen(id));
	}
	evbuffer_add(buffer, payload, strlen(payload));
	evbuffer_add(buffer, "\033\\", 3); /* Include trailing NUL. */
	len = EVBUFFER_LENGTH(buffer);
	result = xmalloc(len);
	memcpy(result, EVBUFFER_DATA(buffer), len);
	evbuffer_free(buffer);
	return (result);
}

static struct clipboard *
clipboard_client(struct window_pane *wp, const char *pw, const char *loc,
    int *denied)
{
	struct client		*c;
	struct clipboard	*cb, *best = NULL;
	size_t			 i;

	*denied = 0;
	TAILQ_FOREACH(c, &clients, entry) {
		cb = c->tty.clipboard;
		if (cb == NULL)
			continue;
		clipboard_sync(&c->tty);
		if (pw != NULL) {
			for (i = 0; i < cb->nrevoked; i++) {
				if (strcmp(pw, cb->revoked[i]) == 0) {
					*denied = 1;
					return (NULL);
				}
			}
			if (cb->grant != NULL && strcmp(pw, cb->grant) == 0) {
				if (cb->owner != wp->id ||
				    strcmp(loc != NULL ? loc : "",
				    cb->location != NULL ? cb->location : "") != 0) {
					*denied = 1;
					return (NULL);
				}
				return (cb);
			}
		}
		if (!cb->supported ||
		    !clipboard_opted_in(c) ||
		    clipboard_pane(cb) == NULL ||
		    !session_has(c->session, wp->window))
			continue;
		if (best == NULL ||
		    timercmp(&c->activity_time,
		    &best->tty->client->activity_time, >))
			best = cb;
	}
	if (pw != NULL && best != NULL && best->deny_unknown_grants) {
		*denied = 1;
		return (NULL);
	}
	return (best);
}

/* Return -1 when the reply is deferred, otherwise the DECRPM state. */
int
clipboard_query(struct window_pane *wp)
{
	struct client		*c;
	struct clipboard	*cb, *pending = NULL;
	size_t			 i;

	if (wp == NULL)
		return (0);
	TAILQ_FOREACH(c, &clients, entry) {
		cb = c->tty.clipboard;
		if (cb == NULL || !clipboard_opted_in(c) ||
		    clipboard_pane(cb) == NULL ||
		    !session_has(c->session, wp->window))
			continue;
		if (cb->supported)
			return ((wp->base.mode & MODE_CLIPBOARD) ? 1 : 2);
		if (cb->probing)
			pending = cb;
	}
	if (pending == NULL)
		return (0);
	for (i = 0; i < pending->nqueries; i++) {
		if (pending->queries[i] == wp->id)
			return (-1);
	}
	if (pending->nqueries == nitems(pending->queries))
		return (0);
	pending->queries[pending->nqueries++] = wp->id;
	return (-1);
}

void
clipboard_request(struct window_pane *wp, const char *body)
{
	struct clipboard_packet	 p = { 0 };
	struct clipboard	*cb = NULL;
	struct client		*c;
	char			*mapped;
	uint32_t		 nonce[4];
	int			 continuation, writing, denied;

	if (wp == NULL)
		goto out;
	if (!clipboard_parse(body, &p)) {
		clipboard_invalid_write(wp);
		goto out;
	}
	if (p.status != NULL) {
		clipboard_invalid_write(wp);
		goto out;
	}
	writing = strcmp(p.type, "write") == 0;
	continuation = strcmp(p.type, "wdata") == 0 ||
	    strcmp(p.type, "walias") == 0;
	if (continuation) {
		TAILQ_FOREACH(c, &clients, entry) {
			cb = c->tty.clipboard;
			if (cb != NULL)
				clipboard_sync(&c->tty);
			if (cb != NULL && cb->writing &&
			    cb->request_pane == wp->id)
				break;
			cb = NULL;
		}
		if (cb == NULL)
			goto out; /* Drain failed/cancelled writes. */
	} else {
		if (!writing && strcmp(p.type, "read") != 0)
			goto out;
		cb = clipboard_client(wp, p.pw, p.loc, &denied);
		if (cb == NULL) {
			clipboard_reply(wp, p.type, p.id,
			    denied ? "EPERM" : "ENOSYS");
			goto out;
		}
		/* Continuations have no independent route: one owner per pane. */
		TAILQ_FOREACH(c, &clients, entry) {
			if (c->tty.clipboard != NULL &&
			    c->tty.clipboard != cb &&
			    c->tty.clipboard->request_pane == wp->id) {
				clipboard_reply(wp, p.type, p.id, "EBUSY");
				goto out;
			}
		}
		if (cb->request_pane != CLIPBOARD_NO_PANE) {
			/* A new write by its owner replaces the previous write. */
			if (!writing || !cb->writing ||
			    cb->request_pane != wp->id) {
				clipboard_reply(wp, p.type, p.id, "EBUSY");
				goto out;
			}
			clipboard_clear_request(cb);
		}
		cb->request_pane = wp->id;
		cb->writing = writing;
		cb->granted = p.pw != NULL && p.name != NULL &&
		    *p.name != '\0' && cb->grant != NULL &&
		    strcmp(p.pw, cb->grant) == 0;
		cb->original_id = p.id != NULL ? xstrdup(p.id) : NULL;
		arc4random_buf(nonce, sizeof nonce);
		xsnprintf(cb->mapped_id, sizeof cb->mapped_id,
		    "tmux-%08x%08x%08x%08x",
		    nonce[0], nonce[1], nonce[2], nonce[3]);
	}
	mapped = clipboard_map(body, cb->mapped_id);
	clipboard_send(cb, mapped, strlen(mapped));
	free(mapped);
	cb->output_pane = wp->id;
	cb->request_time = get_timer();
	clipboard_arm(cb);
out:
	free(p.storage);
}

/*
 * This is an operator-declared peer ordering contract, not a capability
 * inferred from DECRPM. No content read, authority or MIME list reaches a pane.
 */
static int
clipboard_fence_response(struct clipboard *cb, struct clipboard_packet *p)
{
	const char	*payload, *q;
	u_char		*decoded;
	size_t		 size;
	int		 valid = 1;

	if (!cb->closing || !cb->verified || p->id == NULL ||
	    strcmp(p->id, cb->fence_id) != 0)
		return (0);
	if (cb->fence_stage < 0)
		return (1);
	if (strcmp(p->type, "read") != 0 || p->status == NULL) {
		cb->fence_stage = -1;
		return (1);
	}
	if (strcmp(p->status, "OK") == 0 && cb->fence_stage == 0)
		cb->fence_stage = 1;
	else if (strcmp(p->status, "DATA") == 0 &&
	    (cb->fence_stage == 1 || cb->fence_stage == 2) &&
	    p->mime != NULL && strcmp(p->mime, "Lg==") == 0) {
		payload = strchr(p->body, ';');
		if (payload == NULL)
			valid = 0;
		else {
			payload++;
			size = strlen(payload);
			if (size % 4 != 0)
				valid = 0;
			for (q = payload; *q != '\0'; q++) {
				if (!isalnum((u_char)*q) &&
				    strchr("+/=", *q) == NULL)
					valid = 0;
			}
			decoded = xmalloc(size + 1);
			if (b64_pton(payload, decoded, size + 1) < 0)
				valid = 0;
			free(decoded);
		}
		cb->fence_stage = valid ? 2 : -1;
	} else if (strcmp(p->status, "DONE") == 0 && cb->fence_stage == 2)
		cb->fence_stage = 3;
	else
		cb->fence_stage = -1;
	return (1);
}

static void
clipboard_corrupt_response(struct clipboard *cb)
{
	if (cb->closing) {
		if (cb->fence_stage == 1 || cb->fence_stage == 2)
			cb->fence_stage = -1;
		return;
	}
	if (cb->request_pane == CLIPBOARD_NO_PANE && !cb->offer)
		return;
	/* Never forward DONE after silently dropping a chunk of this read. */
	cb->generation++;
	clipboard_cancel_output(cb);
	clipboard_clear_request(cb);
	clipboard_clear_grant(cb);
	if (cb->enabled)
		clipboard_send(cb, "\033[?5522h", 8);
}

static void
clipboard_response(struct clipboard *cb, const char *body)
{
	struct clipboard_packet	 p;
	struct window_pane	*wp;
	char			*mapped;
	int			 done = 0;

	clipboard_sync(cb->tty);
	if (!clipboard_parse(body, &p)) {
		clipboard_corrupt_response(cb);
		goto out;
	}
	if (clipboard_fence_response(cb, &p))
		goto out;
	if (p.id != NULL) {
		if (cb->request_pane == CLIPBOARD_NO_PANE ||
		    strcmp(p.id, cb->mapped_id) != 0)
			goto out;
		if (p.status == NULL ||
		    strcmp(p.type, cb->writing ? "write" : "read") != 0) {
			clipboard_corrupt_response(cb);
			goto out;
		}
		wp = window_pane_find_by_id(cb->request_pane);
		if (wp == NULL || wp->event == NULL) {
			clipboard_clear_request(cb);
			goto out;
		}
		if (cb->writing)
			done = 1;
		else if (strcmp(p.status, "OK") == 0) {
			if (cb->read_started) {
				clipboard_corrupt_response(cb);
				goto out;
			}
			cb->read_started = 1;
		} else if (strcmp(p.status, "DONE") == 0) {
			if (!cb->read_started) {
				clipboard_corrupt_response(cb);
				goto out;
			}
			done = 1;
		} else if (strcmp(p.status, "DATA") == 0) {
			if (!cb->read_started) {
				clipboard_corrupt_response(cb);
				goto out;
			}
			/* Listing/unavailable-only reads do not spend a grant. */
			if (cb->granted && p.mime != NULL &&
			    strcmp(p.mime, "Lg==") != 0)
				clipboard_clear_grant(cb);
		} else {
			if (cb->read_started) {
				clipboard_corrupt_response(cb);
				goto out;
			}
			done = 1;
		}
		mapped = clipboard_map(body, cb->original_id);
		bufferevent_write(wp->event, mapped, strlen(mapped));
		free(mapped);
		cb->request_time = get_timer();
		if (done)
			clipboard_clear_request(cb);
	} else {
		if (p.status == NULL || strcmp(p.type, "read") != 0) {
			if (cb->offer)
				clipboard_corrupt_response(cb);
			goto out;
		}
		if (!cb->enabled || strcmp(p.type, "read") != 0 ||
		    cb->request_pane != CLIPBOARD_NO_PANE)
			goto out;
		wp = clipboard_pane(cb);
		if (wp == NULL || wp->id != cb->owner)
			goto out;
		if (strcmp(p.status, "OK") == 0) {
			clipboard_clear_grant(cb);
			cb->offer = 1;
			cb->grant = p.pw != NULL ? xstrdup(p.pw) : NULL;
			cb->location = p.loc != NULL ? xstrdup(p.loc) : NULL;
			cb->grant_time = get_timer();
		} else if (!cb->offer)
			goto out;
		if (p.pw != NULL &&
		    (cb->grant == NULL || strcmp(p.pw, cb->grant) != 0)) {
			clipboard_corrupt_response(cb);
			goto out;
		}
		if (strcmp(p.status, "DONE") == 0)
			cb->offer = 0;
		mapped = clipboard_map(body, NULL);
		bufferevent_write(wp->event, mapped, strlen(mapped));
		free(mapped);
	}
out:
	free(p.storage);
	clipboard_arm(cb);
}

/*
 * Return -1 for other input, 0 after consuming bytes, 1 while waiting on owned
 * protocol input, or 2 for a prefix using normal escape-time disambiguation. Once
 * OSC5522 is recognized, never fall back to keyboard parsing (including on
 * overflow). Keep a bounded buffer and discard through BEL or ST.
 */
int
clipboard_key(struct tty *tty, const char *buf, size_t len, size_t *used)
{
	struct clipboard	*cb = tty->clipboard;
	struct window_pane	*wp;
	const char		*prefix = "\033]5522;";
	size_t			 i, n;
	int			 state;

	if (cb == NULL)
		return (-1);
	if ((tty->flags & TTY_BRACKETPASTE) && !cb->framing)
		return (-1);
	clipboard_sync(tty);
	wp = window_pane_find_by_id(cb->request_pane);
	if (wp != NULL && wp->event != NULL &&
	    EVBUFFER_LENGTH(wp->event->output) >= CLIPBOARD_WATERMARK) {
		event_del(&tty->event_in);
		cb->input_paused = 1;
		return (1);
	}
	if (!cb->framing) {
		/* Recognize supported/unsupported reports even after timeout. */
		prefix = "\033[?5522;";
		n = strlen(prefix);
		if (len >= 2 && memcmp(buf, prefix, len < n ? len : n) == 0) {
			if (len < n)
				return (cb->closing ? 1 : 2);
			cb->framing = cb->report = 1;
			cb->frame_len = cb->discard = cb->escape = 0;
			cb->frame_generation = cb->generation;
			i = n;
			goto report;
		}
		prefix = "\033]5522;";
		n = strlen(prefix);
		if (memcmp(buf, prefix, len < n ? len : n) != 0) {
			if (!cb->closing)
				return (-1);
			*used = 1; /* Isolated attachment: discard, never keys. */
			return (0);
		}
		if (len < n)
			return (cb->closing ? 1 : (len == 1 ? -1 : 2));
		cb->frame_generation = cb->generation;
		cb->framing = 1;
		cb->report = 0;
		cb->frame_len = cb->discard = cb->escape = 0;
		i = n;
	} else
		i = 0;
	if (cb->report) {
report:
		for (; i < len; i++) {
			if (buf[i] >= 0x40 && buf[i] <= 0x7e) {
				if (!cb->discard && buf[i] == 'y' &&
				    cb->frame_len == 2 && cb->frame[1] == '$' &&
				    cb->frame[0] >= '0' && cb->frame[0] <= '4' &&
				    cb->frame_generation == cb->generation &&
				    cb->probing) {
					state = cb->frame[0] - '0';
					cb->probing = 0;
					cb->supported = state == 1 || state == 2;
					clipboard_queries(cb);
					clipboard_sync(tty);
					clipboard_arm(cb);
				}
				cb->framing = cb->report = 0;
				*used = i + 1;
				clipboard_finish_handoff(cb);
				return (0);
			}
			if (cb->frame_len == 64 || (u_char)buf[i] < 0x20 ||
			    (u_char)buf[i] > 0x7e)
				cb->discard = 1;
			if (!cb->discard)
				cb->frame[cb->frame_len++] = buf[i];
		}
		*used = len;
		return (0);
	}
	for (; i < len; i++) {
		if (buf[i] == '\007' || (cb->escape && buf[i] == '\\')) {
			if (buf[i] == '\007' && cb->escape)
				cb->discard = 1;
			if (!cb->discard &&
			    cb->frame_generation == cb->generation) {
				if (cb->escape && cb->frame_len != 0)
					cb->frame_len--;
				cb->frame[cb->frame_len] = '\0';
				clipboard_response(cb, cb->frame);
			} else if (cb->discard &&
			    cb->frame_generation == cb->generation)
				clipboard_corrupt_response(cb);
			cb->framing = 0;
			clipboard_finish_handoff(cb);
			*used = i + 1;
			return (0);
		}
		cb->escape = buf[i] == '\033';
		if (((u_char)buf[i] < 0x20 && buf[i] != '\033') ||
		    (u_char)buf[i] > 0x7e)
			cb->discard = 1;
		if (cb->frame_len == CLIPBOARD_FRAME_LIMIT)
			cb->discard = 1;
		if (!cb->discard)
			cb->frame[cb->frame_len++] = buf[i];
	}
	*used = len;
	return (0);
}

int
clipboard_blocked(struct window_pane *wp)
{
	struct client		*c;
	struct clipboard	*cb;

	TAILQ_FOREACH(c, &clients, entry) {
		cb = c->tty.clipboard;
		if (cb != NULL && cb->output_pane == wp->id &&
		    EVBUFFER_LENGTH(c->tty.out) >= CLIPBOARD_WATERMARK)
			return (1);
	}
	return (0);
}

void
clipboard_reset_pane(struct window_pane *wp)
{
	struct client		*c;
	struct clipboard	*cb;

	TAILQ_FOREACH(c, &clients, entry) {
		cb = c->tty.clipboard;
		if (cb == NULL)
			continue;
		if (cb->owner == wp->id) {
			cb->generation++;
			if (cb->enabled || cb->request_pane != CLIPBOARD_NO_PANE)
				clipboard_cancel_output(cb);
			cb->enabled = 0;
			cb->owner = CLIPBOARD_NO_PANE;
			clipboard_clear_grant(cb);
		}
		if (cb->request_pane == wp->id) {
			if (cb->owner != CLIPBOARD_NO_PANE) {
				clipboard_cancel_output(cb);
				if (cb->enabled)
					clipboard_send(cb, "\033[?5522h", 8);
			}
			clipboard_clear_request(cb);
		}
	}
}
