// CableTV Remote Control JavaScript

class RemoteControl {
    constructor() {
        this.channelInput = '';
        this.inputTimeout = null;
        this.updateInterval = null;

        this.init();
    }

    init() {
        // Channel up/down buttons
        document.getElementById('btn-up').addEventListener('click', () => this.channelUp());
        document.getElementById('btn-down').addEventListener('click', () => this.channelDown());

        // Number pad
        document.querySelectorAll('.btn.num').forEach(btn => {
            btn.addEventListener('click', () => this.numberPress(btn.dataset.num));
        });

        // Enter/Go button
        document.getElementById('btn-enter').addEventListener('click', () => this.enterChannel());

        // Info button
        document.getElementById('btn-info').addEventListener('click', () => this.showInfo());

        // Keyboard support
        document.addEventListener('keydown', (e) => this.handleKeyboard(e));

        // Load channels and start updates
        this.loadChannels();
        this.updateStatus();
        this.updateInterval = setInterval(() => this.updateStatus(), 2000);
    }

    async apiCall(endpoint, method = 'GET') {
        try {
            const response = await fetch(endpoint, { method });
            return await response.json();
        } catch (error) {
            console.error('API error:', error);
            return null;
        }
    }

    async channelUp() {
        const result = await this.apiCall('/api/channel/up', 'POST');
        if (result && result.success) {
            this.updateStatus();
        }
    }

    async channelDown() {
        const result = await this.apiCall('/api/channel/down', 'POST');
        if (result && result.success) {
            this.updateStatus();
        }
    }

    async showInfo() {
        await this.apiCall('/api/info', 'POST');
    }

    async tuneToChannel(channel) {
        const result = await this.apiCall(`/api/channel/${channel}`, 'POST');
        if (result && result.success) {
            this.updateStatus();
        }
    }

    numberPress(num) {
        // Clear previous timeout
        if (this.inputTimeout) {
            clearTimeout(this.inputTimeout);
        }

        // Add digit to input
        this.channelInput += num;

        // Show preview
        document.getElementById('channel-number').textContent = this.channelInput;

        // Auto-enter after 1.5 seconds of no input
        this.inputTimeout = setTimeout(() => {
            this.enterChannel();
        }, 1500);
    }

    enterChannel() {
        if (this.inputTimeout) {
            clearTimeout(this.inputTimeout);
            this.inputTimeout = null;
        }

        if (this.channelInput) {
            const channel = parseInt(this.channelInput, 10);
            this.channelInput = '';
            this.tuneToChannel(channel);
        }
    }

    handleKeyboard(e) {
        // Number keys
        if (e.key >= '0' && e.key <= '9') {
            e.preventDefault();
            this.numberPress(e.key);
            return;
        }

        switch (e.key) {
            case 'ArrowUp':
            case '+':
                e.preventDefault();
                this.channelUp();
                break;
            case 'ArrowDown':
            case '-':
                e.preventDefault();
                this.channelDown();
                break;
            case 'Enter':
                e.preventDefault();
                this.enterChannel();
                break;
            case 'i':
            case 'I':
                e.preventDefault();
                this.showInfo();
                break;
        }
    }

    async loadChannels() {
        const result = await this.apiCall('/api/channels');
        if (!result || !result.channels) return;

        const container = document.getElementById('channels');
        container.innerHTML = '';

        result.channels.forEach(ch => {
            const btn = document.createElement('button');
            btn.className = 'channel-btn';
            btn.dataset.channel = ch.number;
            btn.innerHTML = `
                <div class="ch-num">${ch.number}</div>
                <div class="ch-name">${ch.name}</div>
            `;
            btn.addEventListener('click', () => this.tuneToChannel(ch.number));
            container.appendChild(btn);
        });
    }

    async updateStatus() {
        const result = await this.apiCall('/api/status');
        if (!result) return;

        // Update channel display
        const channelNum = document.getElementById('channel-number');
        const channelName = document.getElementById('channel-name');
        const nowPlaying = document.getElementById('now-playing');
        const progressFill = document.getElementById('progress-fill');
        const timeInfo = document.getElementById('time-info');

        if (result.channel) {
            // Only update if not in input mode
            if (!this.channelInput) {
                channelNum.textContent = result.channel;
            }
            channelName.textContent = result.channel_name || '';

            // Update active state in channel list
            document.querySelectorAll('.channel-btn').forEach(btn => {
                btn.classList.toggle('active',
                    parseInt(btn.dataset.channel) === result.channel);
            });
        } else {
            if (!this.channelInput) {
                channelNum.textContent = '--';
            }
            channelName.textContent = 'No Signal';
        }

        if (result.playing) {
            nowPlaying.textContent = result.playing.title;

            if (result.position !== null && result.duration) {
                const percent = (result.position / result.duration) * 100;
                progressFill.style.width = `${percent}%`;

                const elapsed = this.formatTime(result.position);
                const remaining = this.formatTime(result.remaining);
                timeInfo.textContent = `${elapsed} / -${remaining}`;
            }
        } else {
            nowPlaying.textContent = '';
            progressFill.style.width = '0%';
            timeInfo.textContent = '';
        }
    }

    formatTime(seconds) {
        if (seconds === null || seconds === undefined) return '--:--';

        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.remote = new RemoteControl();
});
