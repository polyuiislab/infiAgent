/**
 * MLA-V3 Web UI - Frontend JavaScript
 * 
 * Author: Songmiao Wang
 * MLA System: Chenlin Yu, Songmiao Wang
 */

// Global variables
let currentEventSource = null;
let isRunning = false;
let currentHILTask = null;  // Current HIL task: {hil_id, instruction}
let hilCheckInterval = null;  // Interval for checking HIL tasks

// Message save queue (ensures serial saving to avoid concurrency issues)
let saveQueue = [];
let isSaving = false;

// Agent avatar mapping (using Font Awesome icons)
const agentAvatars = {
    'alpha_agent': '<i class="fas fa-robot"></i>',
    'alpha_node': '<i class="fas fa-robot"></i>',  // Legacy support
    'writing_agent': '<i class="fas fa-pen"></i>',
    'researcher': '<i class="fas fa-dna"></i>',
    'data_collection_agent': '<i class="fas fa-chart-bar"></i>',
    'protein_function_evidence_agent': '<i class="fas fa-microscope"></i>',
    'get_searchPdf_by_doi_or_title': '<i class="fas fa-download"></i>',
    'web_search_agent': '<i class="fas fa-search"></i>',
    'default': '<i class="fas fa-robot"></i>'
};

/**
 * Replace emoji with Font Awesome icons in text
 * @param {string} text - Text containing emoji
 * @returns {string} - Text with emoji replaced by HTML icon tags
 */
function replaceEmojiWithIcons(text) {
    if (typeof text !== 'string') return text;
    
    return text
        .replace(/⬇️/g, '<i class="fas fa-download"></i>')
        .replace(/🗑️/g, '<i class="fas fa-trash"></i>')
        .replace(/✕/g, '<i class="fas fa-times"></i>')
        .replace(/✅/g, '<i class="fas fa-check-circle"></i>')
        .replace(/❌/g, '<i class="fas fa-times-circle"></i>')
        .replace(/🔧/g, '<i class="fas fa-wrench"></i>')
        .replace(/📚/g, '<i class="fas fa-book"></i>')
        .replace(/📋/g, '<i class="fas fa-clipboard-list"></i>')
        .replace(/🚀/g, '<i class="fas fa-rocket"></i>')
        .replace(/⏹️/g, '<i class="fas fa-stop"></i>')
        .replace(/📤/g, '<i class="fas fa-upload"></i>')
        .replace(/🔄/g, '<i class="fas fa-sync-alt"></i>')
        .replace(/📁/g, '<i class="fas fa-folder"></i>')
        .replace(/👋/g, '<i class="fas fa-hand-wave"></i>')
        .replace(/⚠️/g, '<i class="fas fa-exclamation-triangle"></i>');
}

// Agent color cache (ensures same agent always gets same color)
const agentColors = {};

/**
 * Generate unique color based on agent name
 * Uses hash function to ensure same name always gets same color
 */
function getAgentColor(agentName) {
    if (agentColors[agentName]) {
        return agentColors[agentName];
    }
    
    // Simple hash function
    let hash = 0;
    for (let i = 0; i < agentName.length; i++) {
        hash = agentName.charCodeAt(i) + ((hash << 5) - hash);
    }
    
    // Generate HSL color (saturation 70-100%, lightness 50-70% for vibrant and visible colors)
    const hue = Math.abs(hash) % 360;
    const saturation = 70 + (Math.abs(hash) % 31); // 70-100%
    const lightness = 50 + (Math.abs(hash) % 21); // 50-70%
    
    // Convert to HSL string
    const color = `hsl(${hue}, ${saturation}%, ${lightness}%)`;
    
    // Cache color
    agentColors[agentName] = color;
    
    return color;
}

// DOM elements
const taskIdInput = document.getElementById('task-id');
const taskSelect = document.getElementById('task-select');
const confirmTaskBtn = document.getElementById('confirm-task-btn');
const clearTaskBtn = document.getElementById('clear-task-btn');
const copyTaskBtn = document.getElementById('copy-task-btn');
const downloadTaskBtn = document.getElementById('download-task-btn');
const configBtn = document.getElementById('config-btn');
// Fixed to use Default system
const AGENT_SYSTEM = 'Default';
const userInput = document.getElementById('user-input');
const sendBtn = document.getElementById('send-btn');
const stopBtn = document.getElementById('stop-btn');
const messagesContainer = document.getElementById('messages');
const statusText = document.getElementById('status-text');
const workspacePath = document.getElementById('workspace-path');

// File browser elements
const fileBrowserPath = document.getElementById('file-browser-path');
const fileTree = document.getElementById('file-tree');
const refreshFilesBtn = document.getElementById('refresh-files-btn');
const uploadFileBtn = document.getElementById('upload-file-btn');
const fileUploadInput = document.getElementById('file-upload-input');
const fileViewer = document.getElementById('file-viewer');
const fileViewerTitle = document.getElementById('file-viewer-title');
const fileViewerContent = document.getElementById('file-viewer-content');
const closeFileBtn = document.getElementById('close-file-btn');
const deleteFileBtn = document.getElementById('delete-file-btn');
const downloadFileBtn = document.getElementById('download-file-btn');

// Current browsing path (for directory navigation)
let currentBrowsePath = '';
let currentViewingFile = null; // Currently viewing file path
let confirmedTaskId = null;  // Currently confirmed taskid

// Check and stop old task
async function checkAndStopOldTask() {
    try {
        // Check status first
        const statusResponse = await fetch('/api/status', {
            credentials: 'include'
        });
        const statusData = await statusResponse.json();
        
        // If there's a running task, stop it automatically
        if (statusData.running) {
            console.log('Detected running task, stopping automatically...');
            await fetch('/api/stop', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                credentials: 'include'
            });
        }
    } catch (error) {
        // Ignore error (server may not be started, etc.)
        console.log('Failed to check task status (may be normal):', error);
    }
}

// Check login status
async function checkAuth() {
    try {
        const response = await fetch('/api/check-auth', {
            credentials: 'include'
        });
        const data = await response.json();
        
        if (!data.logged_in) {
            // Not logged in, redirect to login page
            window.location.href = '/';
            return false;
        }
        
        // Display username
        const usernameDisplay = document.getElementById('username-display');
        if (usernameDisplay) {
            usernameDisplay.textContent = `User: ${data.username || 'polyu'}`;
        }
        
        return true;
    } catch (error) {
        console.error('Failed to check login status:', error);
        return false;
    }
}

// Logout
async function logout() {
    try {
        const response = await fetch('/api/logout', {
            method: 'POST',
            credentials: 'include'
        });
        const data = await response.json();
        
        if (data.success) {
            // Direct redirect without fade-out animation (avoid white screen issue)
            window.location.replace('/');
        }
    } catch (error) {
        console.error('Logout failed:', error);
    }
}

// Initialize
document.addEventListener('DOMContentLoaded', async () => {
    // Check login status
    const isAuthenticated = await checkAuth();
    if (!isAuthenticated) {
        return;  // Not logged in, don't continue initialization
    }
    
    // Logout button event
    const logoutBtn = document.getElementById('logout-btn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', logout);
    }
    
    // Check and stop any existing old task on page load
    await checkAndStopOldTask();
    
    // Restore task_id from localStorage (restore after refresh)
    const savedTaskId = localStorage.getItem('mla_task_id');
    if (savedTaskId) {
        taskIdInput.value = savedTaskId;
        confirmedTaskId = savedTaskId;  // Restore confirmed taskid
    } else {
        // If no saved taskid, clear input (don't set default value)
        taskIdInput.value = '';
    }
    
    updateWorkspacePath();
    // Agent selection removed, always use alpha_agent
    
    // If task_id already has a value，自动加载聊天记录
    const taskId = taskIdInput.value.trim();
    if (taskId && savedTaskId) {
        // Only when there is saved taskid in localStorage
        console.log('DOMContentLoaded: Detected saved taskId =', taskId, '');
        // Ensure welcome message is hidden
        const welcomeMsg = messagesContainer.querySelector('.welcome-message');
        if (welcomeMsg) {
            welcomeMsg.style.display = 'none';
        }
        // Delay to ensure other initialization completes
        setTimeout(async () => {
            console.log('DOMContentLoaded: Start loading chat history');
            await loadChatHistory(taskId);
            // After loading history, if there is history
            // If no history, welcome message should be kept
            const welcomeMsgAfter = messagesContainer.querySelector('.welcome-message');
            if (welcomeMsgAfter) {
                // If no history but welcome message still exists, also remove it (user has entered taskid)
                welcomeMsgAfter.remove();
                console.log('DOMContentLoaded: No history');
            }
        }, 500);
    } else {
        console.log('DOMContentLoaded: No saved taskId');
        // 如果没有保存的taskid
        const welcomeMsg = messagesContainer.querySelector('.welcome-message');
        if (welcomeMsg) {
            welcomeMsg.style.display = '';
        }
    }
    
    // Event listeners
    // Update task list in real-time
    taskIdInput.addEventListener('input', () => {
        updateWorkspacePath();
        // Update task list in real-time
        loadTasks();
    });
    taskIdInput.addEventListener('change', () => {
        updateWorkspacePath();
    });
    taskIdInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            confirmTask();
        }
    });
    confirmTaskBtn.addEventListener('click', confirmTask);
    clearTaskBtn.addEventListener('click', clearTask);
    copyTaskBtn.addEventListener('click', copyTask);
    downloadTaskBtn.addEventListener('click', downloadTask);
    configBtn.addEventListener('click', openConfigModal);
    sendBtn.addEventListener('click', sendMessage);
    stopBtn.addEventListener('click', stopTask);
    userInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
    // Listen to input changes
    userInput.addEventListener('input', updateSendButtonState);
    // Agent selection removed, always use alpha_agent
    
    // Task select box event
    taskSelect.addEventListener('change', (e) => {
        const selectedPath = e.target.value;
        if (selectedPath) {
            taskIdInput.value = selectedPath;
            updateWorkspacePath();
        }
        // Update task list in real-time
        loadTasks();
    });
    
    // Update task list in real-time
    taskSelect.addEventListener('input', () => {
        loadTasks();
    });
    
    // Load task list
    loadTasks();
    
    // Set timer, periodically refresh task list (real-time folder scanning)
    setInterval(() => {
        loadTasks();
    }, 1000); // Refresh every 1 second, real-time update
    
    // When page gains focus
    window.addEventListener('focus', () => {
        loadTasks();
    });
    
    // When mouse hovers over task select box
    taskSelect.addEventListener('mouseenter', () => {
        loadTasks();
    });
    
    // Initialize button state
    updateTaskButtonsState();
    updateSendButtonState(); // Initialize send button state
    
    // Start HIL task checking when task is running
    startHILTaskChecking();
    
    // File browser events
    refreshFilesBtn.addEventListener('click', () => {
        loadFiles(); // Refresh current directory, no parameters
    });
    uploadFileBtn.addEventListener('click', () => {
        fileUploadInput.click();
    });
    fileUploadInput.addEventListener('change', handleFileUpload);
    closeFileBtn.addEventListener('click', () => {
        fileViewer.style.display = 'none';
        currentViewingFile = null;
    });
    deleteFileBtn.addEventListener('click', handleDeleteFile);
    downloadFileBtn.addEventListener('click', handleDownloadFile);
    
    // Initial file list load
    loadFiles();
    
    // Periodically refresh file list
    setInterval(() => {
        if (taskIdInput.value.trim()) {
            loadFiles();
        }
    }, 5000);
    
    // Initialize configuration modal
    initConfigModal();
});

// 加载聊天记录
async function loadChatHistory(taskId, shouldRemoveWelcome = false) {
    if (!taskId) {
        console.log('loadChatHistory: taskId is empty, skip loading');
        return;
    }
    
    console.log('loadChatHistory: Start loading chat history，taskId =', taskId, 'shouldRemoveWelcome =', shouldRemoveWelcome);
    
    try {
        const response = await fetch(`/api/chat/history?task_id=${encodeURIComponent(taskId)}`, {
            credentials: 'include'
        });
        const data = await response.json();
        
        console.log('loadChatHistory: API response:', data);
        
        if (data.error) {
            console.error('Failed to load chat history:', data.error);
            return;
        }
        
        // Save welcome message reference first（如果存在）
        const welcomeMsg = messagesContainer.querySelector('.welcome-message');
        
        // Load history messages
        if (data.messages && data.messages.length > 0) {
            // If there is history, remove welcome message
            if (welcomeMsg) {
                // 如果是用户明确操作（shouldRemoveWelcome=true），用淡出动画
                // If page initialization load, remove directly（不淡出）
                if (shouldRemoveWelcome) {
                    welcomeMsg.classList.add('fade-out');
                    // 返回 Promise，等待淡出动画完成
                    return new Promise((resolve) => {
                        setTimeout(() => {
                            welcomeMsg.remove();
                            // Clear existing messages and load history
                            messagesContainer.innerHTML = '';
                            loadHistoryMessages(data.messages);
                            resolve();
                        }, 300);
                    });
                } else {
                    // 页面初始化，直接移除（不淡出）
                    welcomeMsg.remove();
                    messagesContainer.innerHTML = '';
                    loadHistoryMessages(data.messages);
                }
            } else {
                // No welcome message, clear and load directly
                messagesContainer.innerHTML = '';
                loadHistoryMessages(data.messages);
            }
        } else {
            console.log('loadChatHistory: 没有找到消息（data.messages 为空或长度为 0）');
            // 如果没有历史记录，根据 shouldRemoveWelcome 参数决定是否移除欢迎消息
            // Only remove when user explicitly operates
            if (shouldRemoveWelcome && welcomeMsg) {
                welcomeMsg.classList.add('fade-out');
                setTimeout(() => {
                    welcomeMsg.remove();
                }, 300);
            }
        }
    } catch (error) {
        console.error('Failed to load chat history:', error);
    }
}

// Load history messages（辅助函数）
function loadHistoryMessages(messages) {
    console.log('loadHistoryMessages: found', messages.length, 'messages, start rendering');
    messages.forEach((msg, index) => {
        console.log(`loadHistoryMessages: rendering message ${index + 1}/${messages.length}:`, {
            agent: msg.agent,
            type: msg.type,
            isUser: msg.isUser,
            contentLength: msg.content ? msg.content.length : 0
        });
        // 直接渲染消息，不保存（避免重复）
        try {
            renderMessage(msg.agent, msg.type, msg.content, msg.isUser, false);
        } catch (error) {
            console.error(`loadHistoryMessages: rendering message ${index + 1} failed:`, error, msg);
        }
    });
    
    // Scroll to bottom
    setTimeout(() => {
        messagesContainer.scrollTop = messagesContainer.scrollHeight;
    }, 100);
    console.log('loadHistoryMessages: chat history loaded');
}

// Process save queue
async function processSaveQueue() {
    if (isSaving || saveQueue.length === 0) {
        return;
    }
    
    isSaving = true;
    
    while (saveQueue.length > 0) {
        const { agent, type, displayContent, isUser } = saveQueue.shift();
        await saveChatMessageDirect(agent, type, displayContent, isUser);
    }
    
    isSaving = false;
}

// 直接保存消息到聊天记录（内部函数，由队列调用）
async function saveChatMessageDirect(agent, type, displayContent, isUser) {
    const taskId = taskIdInput.value.trim();
    if (!taskId) {
        console.log('saveChatMessageDirect: taskId is empty, skip saving');
        return;
    }
    
    // 🔧 保存用户看到的内容（美化后的），这样恢复时显示的就是用户之前看到的
    // Note: timestamp will be replaced with sequence number on backend for privacy
    const message = {
        agent: agent,
        type: type,
        content: displayContent,  // 保存用户看到的内容（美化后的）
        isUser: isUser
        // timestamp removed - will be replaced with sequence number on backend
    };
    
    console.log('saveChatMessageDirect: saving message:', {
        agent: agent,
        type: type,
        isUser: isUser,
        contentLength: displayContent ? displayContent.length : 0
    });
    
    try {
        const response = await fetch('/api/chat/save', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include',
            body: JSON.stringify({
                task_id: taskId,
                message: message
            })
        });
        
        const result = await response.json();
        if (result.error) {
            console.error('saveChatMessageDirect: saving failed:', result.error);
        } else {
            console.log('saveChatMessageDirect: saving successful');
        }
    } catch (error) {
        console.error('saveChatMessageDirect: saving chat history failed:', error);
    }
}

// 保存消息到聊天记录（添加到队列，串行处理）
function saveChatMessage(agent, type, displayContent, isUser) {
    // 🔧 添加到队列，确保串行保存，避免并发问题
    saveQueue.push({ agent, type, displayContent, isUser });
    
    // 异步处理队列（不阻塞）
    processSaveQueue().catch(error => {
        console.error('processSaveQueue: processing save queue failed:', error);
        isSaving = false;
    });
}

// 确认任务ID
async function confirmTask() {
    // 如果任务正在运行，不允许确认新任务
    if (isRunning) {
        alert('Task is running, please stop it first');
        return;
    }
    
    const taskId = taskIdInput.value.trim();
    
    if (!taskId) {
        alert('Please enter Task ID');
        return;
    }
    
    confirmTaskBtn.disabled = true;
    confirmTaskBtn.textContent = 'Confirming...';
    
    try {
        const response = await fetch('/api/task/confirm', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include',
            body: JSON.stringify({ task_id: taskId })
        });
        
        const data = await response.json();
        
        if (data.error) {
            alert(`Confirmation failed: ${data.error}`);
        } else {
            // 更新显示
            taskIdInput.value = data.task_id;
            // Save to localStorage
            localStorage.setItem('mla_task_id', data.task_id);
            // 更新已确认的taskid
            confirmedTaskId = data.task_id;
            
            updateWorkspacePath();
            loadFiles();
            
            // 显示成功消息
            const message = data.is_new ? 'New task created' : 'Entered existing task';
            statusText.textContent = message;
            
            // Save welcome message reference first（在加载历史记录前）
            const welcomeMsgBeforeLoad = messagesContainer.querySelector('.welcome-message');
            
            // 先加载聊天记录（如果是现有任务，可能已有历史记录）
            // 传入 shouldRemoveWelcome=true，因为这是用户明确操作，应该移除欢迎消息
            console.log('confirmTask: Task confirmed successfully，taskId =', data.task_id);
            await loadChatHistory(data.task_id, true);
            
            // Check if messages already exist（加载历史记录后）
            const existingMessages = messagesContainer.querySelectorAll('.message');
            const welcomeMsgAfterLoad = messagesContainer.querySelector('.welcome-message');
            
            // Check if same confirmation message already exists
            const messageContent = `${message}: ${data.task_id}`;
            let messageExists = false;
            existingMessages.forEach(msg => {
                const textContent = msg.querySelector('.message-text')?.textContent || '';
                if (textContent.includes(messageContent)) {
                    messageExists = true;
                }
            });
            
            // If welcome message still exists（loadChatHistory 没有处理它，比如没有历史记录的情况）
            // 且是之前就存在的，则用淡出动画移除
            // Note: loadChatHistory already handled有历史记录的情况，这里只处理没有历史记录但欢迎消息还在的情况
            if (welcomeMsgAfterLoad && welcomeMsgAfterLoad === welcomeMsgBeforeLoad && welcomeMsgBeforeLoad && !messageExists) {
                // 如果 loadChatHistory 没有移除它（没有历史记录），这里移除
                if (!welcomeMsgAfterLoad.classList.contains('fade-out')) {
                    welcomeMsgAfterLoad.classList.add('fade-out');
                    setTimeout(() => {
                        welcomeMsgAfterLoad.remove();
                    }, 300);
                }
            }
            
            // If no duplicate, show confirmation message（保存到历史记录，因为用户看到了）
            if (!messageExists) {
                // If welcome message is fading out, slightly delay adding confirmation message
                if (welcomeMsgAfterLoad && welcomeMsgAfterLoad.classList.contains('fade-out')) {
                    setTimeout(() => {
                        addMessage('system', 'info', messageContent, false, true);
                    }, 150);
                } else {
                    addMessage('system', 'info', messageContent, false, true);
                }
            }
            
            // Refresh task list
            loadTasks();
        }
    } catch (error) {
        console.error('Task confirmation failed:', error);
        alert(`Confirmation failed: ${error.message}`);
    } finally {
        confirmTaskBtn.disabled = false;
        confirmTaskBtn.textContent = 'Confirm';
    }
}

// Load task list（带防抖，避免频繁请求）
let loadTasksTimeout = null;
async function loadTasks() {
    // 清除之前的定时器
    if (loadTasksTimeout) {
        clearTimeout(loadTasksTimeout);
    }
    
    // 防抖：如果连续调用，只执行最后一次
    loadTasksTimeout = setTimeout(async () => {
        try {
            const response = await fetch('/api/tasks/list', {
                credentials: 'include'
            });
            
            const data = await response.json();
            
            if (data.error) {
                console.error('Failed to load task list:', data.error);
                return;
            }
            
            // Save currently selected value
            const currentValue = taskSelect.value;
            
            // Check if task list has changed（通过比较数量）
            const currentOptions = Array.from(taskSelect.options).slice(1); // Exclude first "Select existing task"
            const currentTaskNames = currentOptions.map(opt => opt.textContent).sort();
            const newTaskNames = (data.tasks || []).map(task => task.name).sort();
            
            // 如果列表没有变化，不更新DOM（避免闪烁）
            const hasChanged = JSON.stringify(currentTaskNames) !== JSON.stringify(newTaskNames);
            
            if (hasChanged || currentTaskNames.length === 0) {
                // Clear existing options（保留第一个"选择现有任务"选项）
                taskSelect.innerHTML = '<option value="">Select existing task</option>';
                
                // 添加任务选项
                if (data.tasks && data.tasks.length > 0) {
                    data.tasks.forEach(task => {
                        const option = document.createElement('option');
                        option.value = task.path;  // 使用相对路径
                        option.textContent = task.name;
                        taskSelect.appendChild(option);
                    });
                }
                
                // Restore previously selected value（如果还存在）
                if (currentValue) {
                    const optionExists = Array.from(taskSelect.options).some(opt => opt.value === currentValue);
                    if (optionExists) {
                        taskSelect.value = currentValue;
                    }
                }
            }
        } catch (error) {
            console.error('Failed to load task list:', error);
        }
    }, 100); // 100ms 防抖延迟
}

// Update task-related button disabled state
function updateTaskButtonsState() {
    if (isRunning) {
        // When task is running, disable confirm, clear task, and copy task buttons
        // Download task can still work even when task is running
        confirmTaskBtn.disabled = true;
        clearTaskBtn.disabled = true;
        copyTaskBtn.disabled = true;
        confirmTaskBtn.style.opacity = '0.5';
        confirmTaskBtn.style.cursor = 'not-allowed';
        clearTaskBtn.style.opacity = '0.5';
        clearTaskBtn.style.cursor = 'not-allowed';
        copyTaskBtn.style.opacity = '0.5';
        copyTaskBtn.style.cursor = 'not-allowed';
        // Download button remains enabled
        downloadTaskBtn.disabled = false;
        downloadTaskBtn.style.opacity = '1';
        downloadTaskBtn.style.cursor = 'pointer';
    } else {
        // After task stops, restore button state
        confirmTaskBtn.disabled = false;
        clearTaskBtn.disabled = false;
        copyTaskBtn.disabled = false;
        downloadTaskBtn.disabled = false;
        confirmTaskBtn.style.opacity = '1';
        confirmTaskBtn.style.cursor = 'pointer';
        clearTaskBtn.style.opacity = '1';
        clearTaskBtn.style.cursor = 'pointer';
        copyTaskBtn.style.opacity = '1';
        copyTaskBtn.style.cursor = 'pointer';
        downloadTaskBtn.style.opacity = '1';
        downloadTaskBtn.style.cursor = 'pointer';
    }
}

// Update send button state（根据输入框是否有内容或是否有 HIL 任务）
function updateSendButtonState() {
    const hasContent = userInput.value.trim().length > 0;
    
    // If there's a HIL task waiting, enable the button (even if input is empty)
    if (currentHILTask) {
        sendBtn.disabled = false;
        return;
    }
    
    // Only update button state based on input when task is not running
    if (!isRunning) {
        sendBtn.disabled = !hasContent;
    }
}

// 清空任务
async function clearTask() {
    // If task is running, don't allow clearing
    if (isRunning) {
        alert('Task is running, please stop it first');
        return;
    }
    
    const taskId = taskIdInput.value.trim();
    
    if (!taskId) {
        alert('Please enter Task ID first');
        return;
    }
    
    // Confirmation dialog
    const confirmed = confirm(
        `⚠️ Warning: Are you sure you want to clear task "${taskId}" and all its files?\n\n` +
        `This operation will delete all contents in this directory, including:\n` +
        `- All generated files\n` +
        `- Chat history\n` +
        `- Uploaded files\n` +
        `- All other data\n\n` +
        `This operation cannot be undone!`
    );
    
    if (!confirmed) {
        return;
    }
    
    // 二次确认
    const doubleConfirmed = confirm(
        `⚠️ Last confirmation: really delete all files in "${taskId}" directory?\n\n` +
        `Click "Confirm" to immediately execute deletion operation, cannot be undone!`
    );
    
    if (!doubleConfirmed) {
        return;
    }
    
    clearTaskBtn.disabled = true;
    clearTaskBtn.textContent = 'Clearing...';
    
    try {
        const response = await fetch('/api/task/clear', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include',
            body: JSON.stringify({ task_id: taskId })
        });
        
        const data = await response.json();
        
        if (data.error) {
            alert(`Clear failed: ${data.error}`);
        } else {
            // Clear UI
            messagesContainer.innerHTML = '<div class="welcome-message"><p>👋 Welcome to MLA-V3 Web UI</p><p>Please set Task ID and Agent above, then enter a task to start conversation.</p></div>';
            fileTree.innerHTML = '<div class="file-tree-empty">Please set Task ID to view files</div>';
            fileBrowserPath.textContent = 'Path not set';
            currentBrowsePath = '';
            currentViewingFile = null;
            
            // 清空 localStorage 中的 task_id
            localStorage.removeItem('mla_task_id');
            
            // 显示成功消息
            statusText.innerHTML = `<i class="fas fa-check-circle"></i> ${data.message}`;
            alert(`✓ ${data.message}`);
            
            // Refresh task list
            loadTasks();
            // 清空已确认的taskid
            confirmedTaskId = null;
        }
    } catch (error) {
        console.error('Clear task failed:', error);
        alert(`Clear failed: ${error.message}`);
    } finally {
        clearTaskBtn.disabled = false;
        clearTaskBtn.textContent = 'Clear Task';
    }
}

// Copy task function
async function copyTask() {
    // If task is running, don't allow copying
    if (isRunning) {
        alert('Task is running, please stop it first');
        return;
    }
    
    const currentTaskId = taskIdInput.value.trim();
    if (!currentTaskId) {
        alert('Please select a task first');
        return;
    }
    
    // Show input dialog
    const newTaskName = prompt('Enter new task name:');
    if (!newTaskName || !newTaskName.trim()) {
        return; // User cancelled or entered empty
    }
    
    const trimmedName = newTaskName.trim();
    
    // Validate input (check for invalid characters)
    if (trimmedName.includes('..') || trimmedName.includes('/') || trimmedName.includes('\\')) {
        alert('Invalid task name: cannot contain "..", "/", or "\\"');
        return;
    }
    
    copyTaskBtn.disabled = true;
    copyTaskBtn.textContent = 'Copying...';
    
    // Create progress modal
    const progressModal = createProgressModal();
    document.body.appendChild(progressModal);
    
    let progressInterval = null;
    
    try {
        // Start copy operation
        const response = await fetch('/api/task/copy', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include',
            body: JSON.stringify({
                source_task_id: currentTaskId,
                target_task_id: trimmedName
            })
        });
        
        const data = await response.json();
        
        if (data.error) {
            closeProgressModal(progressModal);
            alert(`Copy failed: ${data.error}`);
            return;
        }
        
        // Start polling for progress
        progressInterval = setInterval(async () => {
            try {
                const progressResponse = await fetch(`/api/task/copy/progress?task_id=${encodeURIComponent(trimmedName)}`, {
                    credentials: 'include'
                });
                const progressData = await progressResponse.json();
                
                updateProgressModal(progressModal, progressData);
                
                // If completed or error, stop polling
                if (progressData.status === 'completed' || progressData.status === 'error') {
                    clearInterval(progressInterval);
                    
                    if (progressData.status === 'completed') {
                        // Wait a bit before closing modal and switching
                        setTimeout(async () => {
                            closeProgressModal(progressModal);
                            // Switch to new task
                            taskIdInput.value = data.task_id;
                            await confirmTask();
                            alert(`Task copied successfully! Switched to "${data.task_id}"`);
                        }, 1000);
                    } else {
                        closeProgressModal(progressModal);
                        alert(`Copy failed: ${progressData.message}`);
                    }
                }
            } catch (error) {
                console.error('Failed to get progress:', error);
            }
        }, 500); // Poll every 500ms
        
    } catch (error) {
        if (progressInterval) {
            clearInterval(progressInterval);
        }
        closeProgressModal(progressModal);
        console.error('Copy task failed:', error);
        alert('Copy task failed: ' + error.message);
    } finally {
        copyTaskBtn.disabled = false;
        copyTaskBtn.textContent = 'Copy Task';
    }
}

// Create progress modal
function createProgressModal() {
    const modal = document.createElement('div');
    modal.id = 'copy-progress-modal';
    modal.style.cssText = `
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        background: rgba(0, 0, 0, 0.5);
        display: flex;
        justify-content: center;
        align-items: center;
        z-index: 10000;
    `;
    
    const content = document.createElement('div');
    content.style.cssText = `
        background: white;
        padding: 30px;
        border-radius: 8px;
        min-width: 400px;
        max-width: 600px;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
    `;
    
    const title = document.createElement('h3');
    title.textContent = 'Copying Task...';
    title.style.cssText = 'margin-top: 0; margin-bottom: 20px;';
    
    const progressBarContainer = document.createElement('div');
    progressBarContainer.style.cssText = `
        width: 100%;
        height: 20px;
        background: #f0f0f0;
        border-radius: 10px;
        overflow: hidden;
        margin-bottom: 10px;
    `;
    
    const progressBar = document.createElement('div');
    progressBar.id = 'copy-progress-bar';
    progressBar.style.cssText = `
        height: 100%;
        background: #4CAF50;
        width: 0%;
        transition: width 0.3s ease;
    `;
    
    const progressText = document.createElement('div');
    progressText.id = 'copy-progress-text';
    progressText.style.cssText = 'text-align: center; color: #666; font-size: 14px;';
    progressText.textContent = 'Preparing...';
    
    progressBarContainer.appendChild(progressBar);
    content.appendChild(title);
    content.appendChild(progressBarContainer);
    content.appendChild(progressText);
    modal.appendChild(content);
    
    return modal;
}

// Update progress modal
function updateProgressModal(modal, progressData) {
    const progressBar = modal.querySelector('#copy-progress-bar');
    const progressText = modal.querySelector('#copy-progress-text');
    
    if (progressBar && progressText) {
        const progress = progressData.progress || 0;
        progressBar.style.width = `${progress}%`;
        progressText.textContent = progressData.message || `Progress: ${progress}%`;
    }
}

// Close progress modal
function closeProgressModal(modal) {
    if (modal && modal.parentNode) {
        modal.parentNode.removeChild(modal);
    }
}

// Download task function
async function downloadTask() {
    const currentTaskId = taskIdInput.value.trim();
    if (!currentTaskId) {
        alert('Please select a task first');
        return;
    }
    
    downloadTaskBtn.disabled = true;
    downloadTaskBtn.textContent = 'Downloading...';
    
    try {
        // Create download URL
        const downloadUrl = `/api/task/download?task_id=${encodeURIComponent(currentTaskId)}`;
        
        // Create temporary anchor element and trigger download
        const link = document.createElement('a');
        link.href = downloadUrl;
        // Sanitize task_id for filename
        const safeTaskId = currentTaskId.replace(/\//g, '_').replace(/\\/g, '_').replace(/\.\./g, '_');
        link.download = `${safeTaskId}.zip`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        
        // Show success message after a short delay (to allow download to start)
        setTimeout(() => {
            alert(`Task "${currentTaskId}" download started`);
        }, 500);
    } catch (error) {
        console.error('Download task failed:', error);
        alert('Download task failed: ' + error.message);
    } finally {
        downloadTaskBtn.disabled = false;
        downloadTaskBtn.textContent = 'Download Task';
    }
}

// 更新 workspace 路径显示
function updateWorkspacePath() {
    const taskId = taskIdInput.value.trim();
    workspacePath.textContent = taskId || 'Please set a path for workspace';
    fileBrowserPath.textContent = taskId || 'Please set a path for workspace';
    // 重置浏览路径到根目录
    currentBrowsePath = '';
}

// 加载文件列表
async function loadFiles(path = null) {
    const taskId = taskIdInput.value.trim();
    
    if (!taskId) {
        fileTree.innerHTML = '<div class="file-tree-empty">Please set Task ID to view files</div>';
        currentBrowsePath = '';
        return;
    }
    
    // 如果 path 是事件对象或无效值，忽略它
    if (path && typeof path === 'object' && path.constructor && path.constructor.name === 'PointerEvent') {
        path = null;
    }
    if (path && typeof path !== 'string') {
        path = null;
    }
    
    // 使用指定路径或当前浏览路径或任务ID
    const browsePath = path || currentBrowsePath || taskId;
    currentBrowsePath = browsePath;
    
    // 更新路径显示（显示相对路径，如果为空则显示根目录）
    fileBrowserPath.textContent = browsePath || '/';
    
    try {
        const response = await fetch(`/api/files/list?path=${encodeURIComponent(browsePath)}`, {
            credentials: 'include'
        });
        const data = await response.json();
        
        if (data.error) {
            fileTree.innerHTML = `<div class="file-tree-empty">${data.error}</div>`;
            return;
        }
        
        fileTree.innerHTML = '';
        
        // If not root directory, add parent button（无论目录是否为空）
        // Root directory determination：如果 browsePath 为空或等于 taskId，则认为是根目录
        const isRoot = !browsePath || browsePath === taskId || browsePath === '';
        if (!isRoot) {
            const backItem = document.createElement('div');
            backItem.className = 'file-item';
            backItem.innerHTML = `
                <span class="file-icon"><i class="fas fa-arrow-up"></i></span>
                <span class="file-name">.. (Go back to parent directory)</span>
            `;
            backItem.addEventListener('click', () => {
                // Calculate parent path
                const pathParts = browsePath.split('/').filter(p => p);
                if (pathParts.length > 1) {
                    // Has parent directory
                    pathParts.pop();
                    const parentPath = pathParts.join('/');
                    loadFiles(parentPath);
                } else {
                    // Return to root directory（taskId）
                    loadFiles(taskId);
                }
            });
            fileTree.appendChild(backItem);
        }
        
        if (data.files && data.files.length > 0) {
            data.files.forEach(file => {
                const fileItem = document.createElement('div');
                fileItem.className = 'file-item';
                fileItem.dataset.path = file.path;
                fileItem.dataset.type = file.type;
                
                const icon = file.type === 'directory' ? '<i class="fas fa-folder"></i>' : '<i class="fas fa-file"></i>';
                const size = file.type === 'file' ? ` (${formatFileSize(file.size)})` : '';
                
                fileItem.innerHTML = `
                    <span class="file-icon">${icon}</span>
                    <span class="file-name">${escapeHtml(file.name)}${size}</span>
                    ${file.type === 'file' ? '<button class="file-item-download-btn" title="Download file"><i class="fas fa-download"></i></button>' : ''}
                `;
                
                // 添加下载按钮的事件监听器（如果是文件）
                if (file.type === 'file') {
                    const downloadBtn = fileItem.querySelector('.file-item-download-btn');
                    if (downloadBtn) {
                        downloadBtn.addEventListener('click', (e) => {
                            e.stopPropagation(); // 阻止事件冒泡到文件项
                            downloadFileFromList(file.path, file.name);
                        });
                    }
                }
                
                fileItem.addEventListener('click', (e) => {
                    // 如果点击的是下载按钮，不处理
                    if (e.target.classList.contains('file-item-download-btn') || e.target.closest('.file-item-download-btn')) {
                        return;
                    }
                    // 移除其他选中状态
                    fileTree.querySelectorAll('.file-item').forEach(item => {
                        item.classList.remove('selected');
                    });
                    // 添加选中状态
                    fileItem.classList.add('selected');
                    
                    if (file.type === 'file') {
                        openFile(file.path, file.name);
                    } else {
                        // 点击目录，进入该目录
                        loadFiles(file.path);
                    }
                });
                
                // 右键菜单（下载或删除）
                fileItem.addEventListener('contextmenu', (e) => {
                    e.preventDefault();
                    if (file.type === 'file') {
                        // 文件：显示下载和删除选项
                        const action = confirm(`File: "${file.name}"\n\nClick OK to download, or Cancel to delete.`);
                        if (action === null) {
                            return; // 用户点击了取消对话框
                        } else if (action) {
                            // 用户点击了 OK，下载文件
                            downloadFileFromList(file.path, file.name);
                        } else {
                            // 用户点击了 Cancel，删除文件
                    if (confirm(`Are you sure you want to delete "${file.name}"?`)) {
                        deleteFileOrDir(file.path, file.name);
                            }
                        }
                    } else {
                        // 目录：只显示删除选项
                        if (confirm(`Are you sure you want to delete "${file.name}"?`)) {
                            deleteFileOrDir(file.path, file.name);
                        }
                    }
                });
                
                fileTree.appendChild(fileItem);
            });
        } else {
            // 如果目录为空，显示提示（但保留返回上级按钮）
            const emptyMsg = document.createElement('div');
            emptyMsg.className = 'file-tree-empty';
            emptyMsg.textContent = 'Directory is empty';
            fileTree.appendChild(emptyMsg);
        }
    } catch (error) {
        console.error('Load file list failed:', error);
        fileTree.innerHTML = `<div class="file-tree-empty">Load failed: ${error.message}</div>`;
    }
}

// 检查文件是否是图片
function isImageFile(fileName) {
    if (!fileName) return false;
    const imageExtensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.ico'];
    const lowerFileName = fileName.toLowerCase();
    return imageExtensions.some(ext => lowerFileName.endsWith(ext));
}

// 打开文件
async function openFile(filePath, fileName) {
    try {
        // 清空之前的样式类
        fileViewerContent.classList.remove('image-mode', 'text-mode');
        
        // 检查是否是图片文件
        if (isImageFile(fileName)) {
            // 图片文件：直接使用预览 API URL
            const previewUrl = `/api/files/preview?path=${encodeURIComponent(filePath)}`;
            
            // 设置图片模式样式
            fileViewerContent.classList.add('image-mode');
            
            // 清空内容并显示图片
            fileViewerContent.innerHTML = '';
            const img = document.createElement('img');
            img.src = previewUrl;
            img.alt = fileName;
            img.onerror = function() {
                fileViewerContent.classList.remove('image-mode');
                fileViewerContent.classList.add('text-mode');
                fileViewerContent.innerHTML = `<div style="color: #ff6b6b; padding: 20px; text-align: center;">Failed to load image: ${escapeHtml(fileName)}</div>`;
            };
            img.onload = function() {
                // 图片加载成功，保持 image-mode
            };
            
            fileViewerContent.appendChild(img);
        } else {
            // 文本文件：读取内容并显示
            fileViewerContent.classList.add('text-mode');
            
        const response = await fetch(`/api/files/read?path=${encodeURIComponent(filePath)}`, {
            credentials: 'include'
        });
        const data = await response.json();
        
        if (data.error) {
                fileViewerContent.innerHTML = `<div style="color: #ff6b6b; padding: 20px;">Error: ${escapeHtml(data.error)}</div>`;
        } else {
            fileViewerContent.textContent = data.content;
            }
        }
        
        fileViewerTitle.textContent = fileName;
        currentViewingFile = filePath;
        fileViewer.style.display = 'flex';
    } catch (error) {
        console.error('Read file failed:', error);
        fileViewerContent.classList.remove('image-mode');
        fileViewerContent.classList.add('text-mode');
        fileViewerContent.innerHTML = `<div style="color: #ff6b6b; padding: 20px;">Read failed: ${escapeHtml(error.message)}</div>`;
        fileViewerTitle.textContent = fileName;
        currentViewingFile = filePath;
        fileViewer.style.display = 'flex';
    }
}

// 从文件列表下载文件（全局函数，供 inline onclick 调用）
async function downloadFileFromList(filePath, fileName) {
    try {
        // filePath 已经是相对于用户工作空间的完整路径（包含 task_id）
        // 所以不需要传递 task_id 参数，直接使用路径即可
        const url = `/api/files/download?path=${encodeURIComponent(filePath)}`;
        
        // 使用 fetch 来下载文件，这样可以更好地处理错误
        const response = await fetch(url, {
            method: 'GET',
            credentials: 'include'
        });
        
        // 检查响应状态码
        if (!response.ok) {
            // 如果状态码不是 2xx，尝试解析错误信息
            // 注意：即使 content-type 是 application/json，如果状态码不是 200，也可能是错误
            const contentType = response.headers.get('content-type') || '';
            if (contentType.includes('application/json')) {
                try {
                    const data = await response.json();
                    alert(`Download failed: ${data.error || `HTTP ${response.status}: ${response.statusText}`}`);
                } catch (e) {
                    alert(`Download failed: HTTP ${response.status}: ${response.statusText}`);
                }
            } else {
                alert(`Download failed: HTTP ${response.status}: ${response.statusText}`);
            }
            return;
        }
        
        // 如果状态码是 200，直接下载文件（无论 content-type 是什么，包括 application/json）
        // 因为用户可能就是要下载 JSON 文件
        const blob = await response.blob();
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = fileName || filePath.split('/').pop() || 'download';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(link.href);
    } catch (error) {
        console.error('Download file failed:', error);
        alert(`Download failed: ${error.message}`);
    }
}

// 下载文件（从文件查看器或选中的文件）
async function handleDownloadFile() {
    let filePath = currentViewingFile;
    let fileName = filePath ? filePath.split('/').pop() : null;
    
    // 如果文件查看器中当前没有文件，检查是否有选中的文件
    if (!filePath) {
        const selectedItem = fileTree.querySelector('.file-item.selected');
        if (!selectedItem) {
            alert('Please select a file to download first');
            return;
        }
        // 检查选中的是文件还是目录
        if (selectedItem.dataset.type === 'directory') {
            alert('Cannot download directory. Please select a file.');
            return;
        }
        filePath = selectedItem.dataset.path;
        fileName = selectedItem.querySelector('.file-name')?.textContent?.split(' (')[0] || filePath.split('/').pop();
    }
    
    await downloadFileFromList(filePath, fileName);
}

// 删除文件
async function handleDeleteFile() {
    if (!currentViewingFile) {
        const selectedItem = fileTree.querySelector('.file-item.selected');
        if (!selectedItem) {
            alert('Please select a file to delete first');
            return;
        }
        currentViewingFile = selectedItem.dataset.path;
    }
    
    const fileName = currentViewingFile.split('/').pop() || currentViewingFile;
    if (!confirm(`Are you sure you want to delete "${fileName}"?`)) {
        return;
    }
    
    try {
        const response = await fetch('/api/files/delete', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include',
            body: JSON.stringify({ path: currentViewingFile })
        });
        
        const data = await response.json();
        
        if (data.error) {
            alert(`Delete failed: ${data.error}`);
        } else {
            alert('Deleted successfully');
            // Close file viewer
            fileViewer.style.display = 'none';
            currentViewingFile = null;
            // 刷新文件列表
            loadFiles();
        }
    } catch (error) {
        console.error('Delete file failed:', error);
        alert(`Delete failed: ${error.message}`);
    }
}

// 处理文件上传
async function handleFileUpload(event) {
    const files = event.target.files;
    if (!files || files.length === 0) {
        return;
    }
    
    const taskId = taskIdInput.value.trim();
    if (!taskId) {
        alert('Please set Task ID first');
        return;
    }
    
    // 使用当前浏览路径或任务ID作为目标目录
    const targetDir = currentBrowsePath || taskId;
    
    for (const file of files) {
        try {
            const formData = new FormData();
            formData.append('file', file);
            formData.append('target_dir', targetDir);
            
            const response = await fetch('/api/files/files', {
                method: 'POST',
                credentials: 'include',
                body: formData
            });
            
            const data = await response.json();
            
            if (data.error) {
                alert(`Upload "${file.name}" failed: ${data.error}`);
            } else {
                console.log(`Upload "${file.name}" successful`);
            }
        } catch (error) {
            console.error('Upload file failed:', error);
            alert(`Upload "${file.name}" failed: ${error.message}`);
        }
    }
    
    // 清空文件选择
    event.target.value = '';
    
    // 刷新文件列表
    loadFiles();
}

// 格式化文件大小
function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// 删除文件或目录
async function deleteFileOrDir(filePath, fileName) {
    try {
        const response = await fetch('/api/files/delete', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include',
            body: JSON.stringify({ path: filePath })
        });
        
        const data = await response.json();
        
        if (data.error) {
            alert(`Delete failed: ${data.error}`);
        } else {
            // If deleting currently viewing file，关闭查看器
            if (currentViewingFile === filePath) {
                fileViewer.style.display = 'none';
                currentViewingFile = null;
            }
            // 刷新文件列表
            loadFiles();
        }
    } catch (error) {
        console.error('Delete failed:', error);
        alert(`Delete failed: ${error.message}`);
    }
}

// HTML 转义
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Agent selection removed, always use alpha_agent

// 发送消息
async function sendMessage() {
    const taskId = taskIdInput.value.trim();
    const agentName = 'alpha_agent';  // Always use alpha_agent
    const userInputText = userInput.value.trim();
    const agentSystem = AGENT_SYSTEM;  // Fixed to use Default
    
    if (!taskId) {
        alert('Please enter Task ID');
        return;
    }
    
    // If there's a HIL task waiting, respond to it instead of starting a new task
    if (currentHILTask) {
        await respondToHILTask(userInputText);
        return;
    }
    
    // If input is empty or button is disabled, return directly（不弹出提示）
    if (!userInputText || sendBtn.disabled) {
        return;
    }
    
    if (isRunning) {
        alert('A task is already running, please wait for it to complete');
        return;
    }
    
    // If taskid not confirmed, auto-confirm first
    if (confirmedTaskId !== taskId) {
        console.log('sendMessage: taskid not confirmed, auto-confirm first');
        try {
            // Call confirmation logic（但不显示确认消息，因为用户没有点击确定按钮）
            const response = await fetch('/api/task/confirm', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                credentials: 'include',
                body: JSON.stringify({ task_id: taskId })
            });
            
            const data = await response.json();
            
            if (data.error) {
                alert(`Task confirmation failed: ${data.error}`);
                return;
            }
            
            // 更新已确认的taskid
            confirmedTaskId = data.task_id;
            // 更新localStorage
            localStorage.setItem('mla_task_id', data.task_id);
            // 更新工作空间路径和文件列表
            updateWorkspacePath();
            loadFiles();
            // 加载聊天记录（静默加载，不显示确认消息）
            await loadChatHistory(data.task_id, true);
        } catch (error) {
            console.error('Auto-confirm task failed:', error);
            alert(`Auto-confirm task failed: ${error.message}`);
            return;
        }
    }
    
    // Disable input
    isRunning = true;
    sendBtn.disabled = true;
    sendBtn.style.display = 'none';
    stopBtn.style.display = 'inline-block';
    userInput.disabled = true;
    statusText.textContent = 'Running...';
    statusText.style.color = '';
    updateTaskButtonsState(); // Update task button state
    
    // Start checking for HIL tasks
    startHILTaskChecking();
    
    // Remove welcome message with fade-out animation
    const welcomeMsg = messagesContainer.querySelector('.welcome-message');
    if (welcomeMsg) {
        welcomeMsg.classList.add('fade-out');
        setTimeout(() => {
            welcomeMsg.remove();
            // Wait for animation to complete before adding user message
            addMessage('user', 'user', userInputText, true, true);
        }, 300);
    } else {
        // 如果没有欢迎消息，直接添加用户消息
        addMessage('user', 'user', userInputText, true, true);
    }
    
    // 清空输入框
    userInput.value = '';
    
    // 启动 SSE 连接
    startSSEConnection(taskId, agentName, userInputText, agentSystem);
}

// 停止任务
async function stopTask() {
    if (!isRunning) {
        return;
    }
    
    try {
        const response = await fetch('/api/stop', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include'
        });
        
        const data = await response.json();
        
        if (data.error) {
            alert(`Stop failed: ${data.error}`);
        } else {
            statusText.textContent = 'Stopping...';
            // Remove all loading animations
            removeAllLoadingAnimations();
            
            // 关闭 SSE 连接
            if (currentEventSource) {
                currentEventSource.close();
                currentEventSource = null;
            }
            // Reset state
            isRunning = false;
            
            // Stop HIL checking
            stopHILTaskChecking();
            clearHILState();
            sendBtn.disabled = false;
            sendBtn.style.display = 'inline-block';
            stopBtn.style.display = 'none';
            userInput.disabled = false;
            statusText.textContent = 'Stopped';
            updateTaskButtonsState(); // Update task button state
            updateSendButtonState(); // Update send button state
        }
    } catch (error) {
        console.error('Stop task failed:', error);
        alert(`Stop failed: ${error.message}`);
    }
}

// 启动 SSE 连接
function startSSEConnection(taskId, agentName, userInputText, agentSystem) {
    // 关闭现有连接
    if (currentEventSource) {
        currentEventSource.close();
    }
    
    // 使用 POST 方法（通过 fetch）进行流式读取
    fetch('/api/run', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        credentials: 'include',
        body: JSON.stringify({
            task_id: taskId,
            agent_name: agentName,
            user_input: userInputText,
            agent_system: agentSystem
        })
    }).then(response => {
        if (!response.ok) {
            return response.json().then(data => {
                throw new Error(data.error || `HTTP error! status: ${response.status}`);
            });
        }
        
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        
        function readStream() {
            reader.read().then(({ done, value }) => {
                if (done) {
                    // Task completed
                isRunning = false;
                sendBtn.disabled = false;
                sendBtn.style.display = 'inline-block';
                stopBtn.style.display = 'none';
                userInput.disabled = false;
                statusText.textContent = 'Ready';
                updateTaskButtonsState(); // Update task button state
                updateSendButtonState(); // Update send button state
                    return;
                }
                
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || ''; // 保留最后不完整的行
                
                for (const line of lines) {
                    if (line.trim() === '' || line.startsWith(':')) {
                        continue; // 跳过空行和心跳
                    }
                    
                    if (line.startsWith('data: ')) {
                        try {
                            const jsonStr = line.slice(6);
                            const data = JSON.parse(jsonStr);
                            // JSON.parse 应该已经将 \n 转换为真正的换行符
                            // 但如果 content 中还有字符串形式的 \n，需要额外处理
                            if (data.content && typeof data.content === 'string') {
                                // 确保字符串中的 \n 是真正的换行符（JSON.parse 应该已经处理了）
                                // 但如果还有转义的 \n（即 \\n），需要转换
                                data.content = data.content.replace(/\\n/g, '\n');
                                data.content = data.content.replace(/\\r\\n/g, '\r\n');
                                data.content = data.content.replace(/\\r/g, '\r');
                            }
                            handleSSEMessage(data);
                        } catch (e) {
                            console.error('Parse SSE message failed:', e, line);
                        }
                    }
                }
                
                readStream();
            }).catch(error => {
                console.error('Read stream failed:', error);
                // Remove all loading animations
                removeAllLoadingAnimations();
                
                isRunning = false;
                sendBtn.disabled = false;
                sendBtn.style.display = 'inline-block';
                stopBtn.style.display = 'none';
                userInput.disabled = false;
                statusText.textContent = 'Error';
                updateTaskButtonsState(); // Update task button state
                updateSendButtonState(); // Update send button state
                showMessage('system', 'error', `Connection error: ${error.message}`);
            });
        }
        
        readStream();
    }).catch(error => {
        console.error('请求失败:', error);
        // Remove all loading animations
        removeAllLoadingAnimations();
        
        isRunning = false;
        sendBtn.disabled = false;
        sendBtn.style.display = 'inline-block';
        stopBtn.style.display = 'none';
        userInput.disabled = false;
        statusText.textContent = 'Error';
        showMessage('system', 'error', `Request failed: ${error.message}`);
    });
}

// 处理 SSE 消息
function handleSSEMessage(data) {
    const type = data.type || 'info';
    const agent = data.agent || 'unknown';
    const content = data.content || '';
    
    if (type === 'end') {
        // Remove all loading animations
        removeAllLoadingAnimations();
        
        isRunning = false;
        sendBtn.disabled = false;
        sendBtn.style.display = 'inline-block';
        stopBtn.style.display = 'none';
        userInput.disabled = false;
        statusText.textContent = 'Completed';
        updateTaskButtonsState(); // Update task button state
        updateSendButtonState(); // Update send button state
        // Stop HIL checking when task ends
        stopHILTaskChecking();
        // Clear HIL state
        clearHILState();
        // Refresh file list when task completes
        loadFiles();
    } else {
        // All messages from SSE are agent messages（isUser = false，保存到历史记录）
        addMessage(agent, type, content, false, true);
        
        // Check if this is a human_in_loop tool call - trigger immediate check
        if (type === 'tool_call' && content.includes('human_in_loop')) {
            // Trigger immediate check (don't wait for polling interval)
            setTimeout(() => checkHILTask(), 500);  // Small delay to allow tool server to register the task
        }
    }
}

// 移除所有消息的加载动画
function removeAllLoadingAnimations() {
    const loadingMessages = messagesContainer.querySelectorAll('.message.loading');
    loadingMessages.forEach(msg => {
        msg.classList.remove('loading');
    });
}

// 添加消息到界面
function addMessage(agent, type, content, isUser = false, saveToHistory = true) {
    // 如果是新消息且任务正在运行，移除之前消息的加载动画
    if (isRunning && !isUser) {
        removeAllLoadingAnimations();
    }
    
    const messageDiv = document.createElement('div');
    // Add different class based on whether it is user message
    const messageClass = isUser ? 'user' : 'agent';
    messageDiv.className = `message ${messageClass} type-${type}`;
    
    // 如果任务正在运行且不是用户消息，添加加载动画
    if (isRunning && !isUser) {
        messageDiv.classList.add('loading');
    }
    
    // Avatar
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    if (isUser) {
        // 用户头像
        avatar.innerHTML = '<i class="fas fa-user"></i>';
        avatar.style.background = 'linear-gradient(135deg, #4ec9b0 0%, #38f9d7 100%)';
    } else {
        // Agent 头像
        avatar.style.background = getAgentColor(agent);
        avatar.innerHTML = agentAvatars[agent] || agentAvatars['default'];
    }
    messageDiv.appendChild(avatar);
    
    // 内容
    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    
    // 头部
    const header = document.createElement('div');
    header.className = 'message-header';
    
    const agentSpan = document.createElement('span');
    agentSpan.className = 'message-agent';
    agentSpan.textContent = agent;
    header.appendChild(agentSpan);
    
    // Timestamp display removed for privacy
    // const timeSpan = document.createElement('span');
    // timeSpan.className = 'message-time';
    // timeSpan.textContent = new Date().toLocaleTimeString('zh-CN');
    // header.appendChild(timeSpan);
    
    contentDiv.appendChild(header);
    
    // 文本（美化显示，去掉 { }）
    const textDiv = document.createElement('div');
    textDiv.className = 'message-text';
    
    // 美化内容：去掉所有的 { } 和多余的空白
    let displayContent = content;
    
    if (typeof displayContent === 'string') {
        // 1. 先处理转义字符：将字符串形式的转义字符转换为实际字符
        // 注意：顺序很重要，先处理复合转义序列，再处理简单转义
        // 可能需要循环处理，因为可能有双重转义的情况
        
        let previousContent = '';
        // 循环处理，直到没有更多转义字符需要处理
        while (displayContent !== previousContent) {
            previousContent = displayContent;
            
            // 先处理换行符（复合序列，按长度从长到短）
            displayContent = displayContent.replace(/\\r\\n/g, '\r\n');
            displayContent = displayContent.replace(/\\n/g, '\n');
            displayContent = displayContent.replace(/\\r/g, '\r');
            // 处理转义的制表符
            displayContent = displayContent.replace(/\\t/g, '\t');
            // 处理转义的双引号（优先处理，因为代码中常见）
            displayContent = displayContent.replace(/\\"/g, '"');
            // 处理转义的单引号
            displayContent = displayContent.replace(/\\'/g, "'");
            // 处理行尾的反斜杠（用于续行）
            displayContent = displayContent.replace(/\\\s*\n/g, '\n');
            // 处理其他转义字符
            displayContent = displayContent.replace(/\\f/g, '\f');
            displayContent = displayContent.replace(/\\b/g, '\b');
            displayContent = displayContent.replace(/\\v/g, '\v');
            // 最后处理转义的反斜杠（单独的反斜杠，不是转义序列的一部分）
            // 使用负向前瞻，确保不是转义序列的一部分
            displayContent = displayContent.replace(/\\(?![nrtfbv"'\\])/g, '');
        }
        
        // 2. 去除 markdown 格式的加粗标记 **XXX** -> XXX
        // 先处理双星号（加粗），使用非贪婪匹配
        displayContent = displayContent.replace(/\*\*([^*]+?)\*\*/g, '$1');
        // 再处理单星号（斜体），但要避免匹配已经处理过的和数学表达式
        // 只匹配不在代码块中的单星号
        displayContent = displayContent.replace(/(?<![*\\])\*([^*\n]+?)\*(?![*])/g, '$1');
    }
    
    // 如果是参数或 final_output 类型，去掉 { } 并美化
    if (type === 'params' || type === 'final_output') {
        // 移除 "参数:" 或 "final_output:" 前缀
        displayContent = displayContent.replace(/^📋\s*参数:\s*/i, '');
        displayContent = displayContent.replace(/^final_output:\s*/i, '');
        
        // 去掉所有的 { } 字符
        displayContent = displayContent.replace(/\{|\}/g, '');
        
        // 清理多余的空白和换行（保留单个换行）
        displayContent = displayContent.replace(/\n\s*\n\s*\n/g, '\n\n'); // 多个连续换行（3个以上）合并为两个
        displayContent = displayContent.replace(/^\s+|\s+$/gm, ''); // 去掉每行首尾空白
        // 对于 final_output，确保调用信息和结果之间有换行
        if (type === 'final_output') {
            // 如果调用信息和结果连在一起（没有换行），添加换行
            // 匹配模式：工具调用信息后直接跟文本（没有换行）
            displayContent = displayContent.replace(/(\] calls tool: final_output)([^\n])/g, '$1\n\n$2');
        }
        displayContent = displayContent.trim();
    } else {
        // 其他类型也去掉 { }（如果有的话）
        displayContent = displayContent.replace(/\{|\}/g, '');
    }
    
    // 先转义 HTML 以确保安全性
    const escapedContent = escapeHtml(displayContent);
    
    // 然后替换 emoji 为图标（在转义之后，这样图标标签不会被转义）
    // 注意：转义后的 emoji 仍然是原样，所以可以直接替换
    const finalContent = replaceEmojiWithIcons(escapedContent);
    
    textDiv.innerHTML = finalContent;
    contentDiv.appendChild(textDiv);
    
    messageDiv.appendChild(contentDiv);
    
    // 添加到容器
    messagesContainer.appendChild(messageDiv);
    
    // Scroll to bottom
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
    
    // 保存到聊天记录（如果需要）
    // 🔧 保存用户看到的内容（美化后的），而不是原始内容
    if (saveToHistory) {
        saveChatMessage(agent, type, displayContent, isUser);
    }
}

// 渲染消息（不保存到历史记录，用于加载历史记录时使用）
function renderMessage(agent, type, content, isUser, saveToHistory = false) {
    addMessage(agent, type, content, isUser, saveToHistory);
}

// 显示系统消息（保存到历史记录，因为用户看到了）
function showMessage(agent, type, content) {
    addMessage(agent, type, content, false, true);
}

// HIL (Human-in-Loop) Task Management

// Start checking for HIL tasks
function startHILTaskChecking() {
    // Clear any existing interval
    stopHILTaskChecking();
    
    // Use longer polling interval (10 seconds) as fallback
    // Most checks will be triggered by tool_call events, so we don't need frequent polling
    hilCheckInterval = setInterval(checkHILTask, 10000);
}

// Stop checking for HIL tasks
function stopHILTaskChecking() {
    if (hilCheckInterval) {
        clearInterval(hilCheckInterval);
        hilCheckInterval = null;
    }
}

// Check for pending HIL tasks
async function checkHILTask() {
    // Only check when task is running
    if (!isRunning) {
        return;
    }
    
    const taskId = taskIdInput.value.trim();
    if (!taskId) {
        return;
    }
    
    try {
        const response = await fetch('/api/hil/check', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include',
            body: JSON.stringify({ task_id: taskId })
        });
        
        const data = await response.json();
        
        if (data.found && data.hil_id) {
            // New HIL task detected
            if (!currentHILTask || currentHILTask.hil_id !== data.hil_id) {
                currentHILTask = {
                    hil_id: data.hil_id,
                    instruction: data.instruction
                };
                
                // Enable input and button, show red blinking effect
                userInput.disabled = false;
                userInput.classList.add('hil-waiting');
                userInput.placeholder = '🔔 ' + (data.instruction || 'Waiting for your response...');
                updateSendButtonState();
                
                // Show status message
                statusText.textContent = '🔔 HIL Task Waiting';
                statusText.style.color = '#ff6b6b';
                
                // Once HIL task is found, check more frequently (every 2 seconds) until responded
                // This ensures we don't miss the completion or status changes
                stopHILTaskChecking();
                hilCheckInterval = setInterval(checkHILTask, 2000);
            }
        } else {
            // No HIL task, clear state if previously set
            if (currentHILTask) {
                clearHILState();
                // Reset to slower polling once HIL is cleared
                stopHILTaskChecking();
                hilCheckInterval = setInterval(checkHILTask, 10000);
            }
        }
    } catch (error) {
        // Silently fail - tool server may be unavailable
        console.error('Check HIL task failed:', error);
    }
}

// Respond to HIL task
async function respondToHILTask(responseText) {
    if (!currentHILTask) {
        return;
    }
    
    const hilId = currentHILTask.hil_id;
    
    try {
        // Add user message to chat
        addMessage('user', 'user', responseText, true, true);
        
        // Send HIL response
        const response = await fetch('/api/hil/respond', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include',
            body: JSON.stringify({
                hil_id: hilId,
                response: responseText
            })
        });
        
        const data = await response.json();
        
        if (data.success) {
            // Clear input
            userInput.value = '';
            
            // Clear HIL state
            clearHILState();
            
            // Show success message
            statusText.textContent = '✅ HIL Response Sent';
            statusText.style.color = '#51cf66';
            setTimeout(() => {
                if (isRunning) {
                    statusText.textContent = 'Running...';
                    statusText.style.color = '';
                }
            }, 2000);
        } else {
            alert(`Failed to respond to HIL task: ${data.error || 'Unknown error'}`);
        }
    } catch (error) {
        alert(`Failed to respond to HIL task: ${error.message}`);
    }
}

// Clear HIL state
function clearHILState() {
    currentHILTask = null;
    userInput.classList.remove('hil-waiting');
    userInput.placeholder = 'Enter task description...';
    userInput.disabled = isRunning;  // Disable if task is running (and no HIL)
    updateSendButtonState();
    statusText.style.color = '';
    
    // Reset polling interval to slower rate when HIL is cleared
    if (isRunning) {
        stopHILTaskChecking();
        hilCheckInterval = setInterval(checkHILTask, 10000);
    }
}

// Configuration Modal Functions
let currentConfigFile = 'llm_config.yaml';
let currentConfigType = 'run_env'; // 'run_env' or 'agent'

// Open configuration modal
function openConfigModal() {
    const modal = document.getElementById('config-modal');
    modal.style.display = 'flex';
    loadConfigFileLists();
    // Load first file from run_env if available
    loadConfigFile(currentConfigFile, currentConfigType);
    // Switch to editor tab by default
    switchConfigTab('editor');
}

// Close configuration modal
function closeConfigModal() {
    const modal = document.getElementById('config-modal');
    modal.style.display = 'none';
}

// Load configuration file lists for both sections
async function loadConfigFileLists() {
    // Load run_env config files
    try {
        const runEnvResponse = await fetch('/api/config/list?type=run_env', {
            credentials: 'include'
        });
        const runEnvData = await runEnvResponse.json();
        
        const runEnvList = document.getElementById('run-env-config-list');
        runEnvList.innerHTML = '';
        
        if (runEnvData.files && runEnvData.files.length > 0) {
            let firstFile = null;
            runEnvData.files.forEach((file, index) => {
                const item = document.createElement('div');
                item.className = 'config-file-item';
                item.dataset.file = file.name;
                item.dataset.type = 'run_env';
                
                // Set icon based on filename
                let icon = 'fas fa-file-code';
                if (file.name.includes('llm')) icon = 'fas fa-brain';
                else if (file.name.includes('tool')) icon = 'fas fa-tools';
                else if (file.name.includes('api')) icon = 'fas fa-plug';
                else if (file.name.includes('gemini')) icon = 'fas fa-robot';
                
                item.innerHTML = `<i class="${icon}"></i> ${file.name}`;
                item.addEventListener('click', () => {
                    currentConfigFile = file.name;
                    currentConfigType = 'run_env';
                    loadConfigFile(file.name, 'run_env');
                });
                runEnvList.appendChild(item);
                
                // Remember first file
                if (index === 0) {
                    firstFile = file.name;
                }
            });
            
            // Auto-load first file if no file is currently selected
            if (!currentConfigFile || currentConfigType !== 'run_env') {
                if (firstFile) {
                    currentConfigFile = firstFile;
                    currentConfigType = 'run_env';
                    loadConfigFile(firstFile, 'run_env');
                }
            }
        } else {
            runEnvList.innerHTML = '<div class="config-file-empty">No files found</div>';
        }
    } catch (error) {
        console.error('Failed to load run_env config files:', error);
    }
    
    // Load agent config files
    try {
        const agentResponse = await fetch('/api/config/list?type=agent', {
            credentials: 'include'
        });
        const agentData = await agentResponse.json();
        
        const agentList = document.getElementById('agent-config-list');
        agentList.innerHTML = '';
        
        if (agentData.files && agentData.files.length > 0) {
            agentData.files.forEach(file => {
                const item = document.createElement('div');
                item.className = 'config-file-item';
                item.dataset.file = file.name;
                item.dataset.type = 'agent';
                
                // Set icon based on filename
                let icon = 'fas fa-file-code';
                if (file.name.includes('level_0')) icon = 'fas fa-wrench';
                else if (file.name.includes('level_1')) icon = 'fas fa-layer-group';
                else if (file.name.includes('level_2')) icon = 'fas fa-sitemap';
                else if (file.name.includes('level_3')) icon = 'fas fa-crown';
                else if (file.name.includes('judge')) icon = 'fas fa-gavel';
                else if (file.name.includes('prompts')) icon = 'fas fa-comments';
                
                item.innerHTML = `<i class="${icon}"></i> ${file.name}`;
                item.addEventListener('click', () => {
                    currentConfigFile = file.name;
                    currentConfigType = 'agent';
                    loadConfigFile(file.name, 'agent');
                });
                agentList.appendChild(item);
            });
        } else {
            agentList.innerHTML = '<div class="config-file-empty">No files found</div>';
        }
    } catch (error) {
        console.error('Failed to load agent config files:', error);
    }
}

// Load configuration file
async function loadConfigFile(filename, type = 'run_env') {
    const textarea = document.getElementById('config-editor-textarea');
    const fileNameSpan = document.getElementById('config-file-name');
    const statusDiv = document.getElementById('config-status');
    
    // Update active file item
    document.querySelectorAll('.config-file-item').forEach(item => {
        item.classList.remove('active');
        if (item.dataset.file === filename && item.dataset.type === type) {
            item.classList.add('active');
        }
    });
    
    fileNameSpan.textContent = filename;
    textarea.value = 'Loading...';
    statusDiv.textContent = '';
    statusDiv.className = 'config-status';
    
    try {
        const response = await fetch(`/api/config/read?file=${encodeURIComponent(filename)}&type=${encodeURIComponent(type)}`, {
            credentials: 'include'
        });
        
        const data = await response.json();
        
        if (data.error) {
            textarea.value = '';
            statusDiv.textContent = `Error: ${data.error}`;
            statusDiv.className = 'config-status error';
        } else {
            textarea.value = data.content;
            statusDiv.textContent = 'File loaded successfully';
            statusDiv.className = 'config-status success';
            setTimeout(() => {
                statusDiv.textContent = '';
            }, 2000);
        }
    } catch (error) {
        textarea.value = '';
        statusDiv.textContent = `Failed to load file: ${error.message}`;
        statusDiv.className = 'config-status error';
    }
}

// Save configuration file
async function saveConfigFile() {
    const textarea = document.getElementById('config-editor-textarea');
    const fileNameSpan = document.getElementById('config-file-name');
    const statusDiv = document.getElementById('config-status');
    const saveBtn = document.getElementById('save-config-btn');
    
    const filename = fileNameSpan.textContent;
    const content = textarea.value;
    
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';
    statusDiv.textContent = 'Saving...';
    statusDiv.className = 'config-status';
    
    try {
        const response = await fetch('/api/config/save', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include',
            body: JSON.stringify({
                file: filename,
                content: content,
                type: currentConfigType
            })
        });
        
        const data = await response.json();
        
        if (data.error) {
            statusDiv.textContent = `Error: ${data.error}`;
            statusDiv.className = 'config-status error';
        } else {
            statusDiv.textContent = data.message || 'Configuration saved successfully';
            statusDiv.className = 'config-status success';
        }
    } catch (error) {
        statusDiv.textContent = `Failed to save: ${error.message}`;
        statusDiv.className = 'config-status error';
    } finally {
        saveBtn.disabled = false;
        saveBtn.innerHTML = '<i class="fas fa-save"></i> Save';
    }
}

// Switch config tab
function switchConfigTab(tabName) {
    // Update tab buttons
    document.querySelectorAll('.config-tab').forEach(tab => {
        tab.classList.remove('active');
        if (tab.dataset.tab === tabName) {
            tab.classList.add('active');
        }
    });
    
    // Update tab content
    document.querySelectorAll('.config-tab-content').forEach(content => {
        content.classList.remove('active');
    });
    
    if (tabName === 'editor') {
        document.getElementById('tab-editor').classList.add('active');
        document.getElementById('config-editor-actions').style.display = 'flex';
    } else if (tabName === 'tree') {
        document.getElementById('tab-tree').classList.add('active');
        document.getElementById('config-editor-actions').style.display = 'none';
        loadAgentTree();
    }
}

// Load and render agent tree
async function loadAgentTree() {
    const container = document.getElementById('agent-tree-container');
    container.innerHTML = '<div class="agent-tree-loading">Loading agent tree...</div>';
    
    try {
        const response = await fetch('/api/config/agent-tree', {
            credentials: 'include'
        });
        
        const data = await response.json();
        
        if (data.error) {
            container.innerHTML = `<div class="agent-tree-error">Error: ${data.error}</div>`;
            return;
        }
        
        // Render tree
        container.innerHTML = '';
        if (data.trees && data.trees.length > 0) {
            data.trees.forEach(tree => {
                const treeElement = renderAgentTreeNode(tree, 0);
                container.appendChild(treeElement);
            });
        } else {
            container.innerHTML = '<div class="agent-tree-empty">No agents found</div>';
        }
    } catch (error) {
        container.innerHTML = `<div class="agent-tree-error">Failed to load agent tree: ${error.message}</div>`;
    }
}

// Render a single agent tree node
function renderAgentTreeNode(node, depth = 0) {
    const nodeDiv = document.createElement('div');
    nodeDiv.className = 'agent-tree-node';
    
    // Node content
    const content = document.createElement('div');
    content.className = 'agent-tree-node-content';
    content.style.paddingLeft = `${depth * 24}px`;
    
    // Level badge
    const levelBadge = document.createElement('span');
    levelBadge.className = `agent-tree-level level-${node.level}`;
    levelBadge.textContent = `L${node.level}`;
    
    // Agent name
    const nameSpan = document.createElement('span');
    nameSpan.className = 'agent-tree-name';
    nameSpan.textContent = node.name;
    
    // Expand/collapse button (only if has children)
    let expandBtn = null;
    if (node.children && node.children.length > 0) {
        expandBtn = document.createElement('button');
        expandBtn.className = 'agent-tree-expand';
        expandBtn.innerHTML = '<i class="fas fa-chevron-down"></i>';
    }
    
    content.appendChild(levelBadge);
    content.appendChild(nameSpan);
    if (expandBtn) {
        content.appendChild(expandBtn);
    }
    
    nodeDiv.appendChild(content);
    
    // Children container
    const childrenDiv = document.createElement('div');
    childrenDiv.className = 'agent-tree-children';
    
    // Render child agents
    if (node.children && node.children.length > 0) {
        node.children.forEach(child => {
            const childElement = renderAgentTreeNode(child, depth + 1);
            childrenDiv.appendChild(childElement);
        });
    }
    
    nodeDiv.appendChild(childrenDiv);
    
    // Toggle expand/collapse
    if (expandBtn) {
        let isExpanded = true;
        expandBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            isExpanded = !isExpanded;
            if (isExpanded) {
                childrenDiv.style.display = '';
                expandBtn.innerHTML = '<i class="fas fa-chevron-down"></i>';
            } else {
                childrenDiv.style.display = 'none';
                expandBtn.innerHTML = '<i class="fas fa-chevron-right"></i>';
            }
        });
    }
    
    return nodeDiv;
}

// Initialize configuration modal event listeners (called in main DOMContentLoaded)
function initConfigModal() {
    // Close button
    const closeConfigBtn = document.getElementById('close-config-btn');
    if (closeConfigBtn) {
        closeConfigBtn.addEventListener('click', closeConfigModal);
    }
    
    // Tab switching
    document.querySelectorAll('.config-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            const tabName = tab.dataset.tab;
            switchConfigTab(tabName);
        });
    });
    
    // Save button
    const saveConfigBtn = document.getElementById('save-config-btn');
    if (saveConfigBtn) {
        saveConfigBtn.addEventListener('click', saveConfigFile);
    }
    
    // Reload button
    const reloadConfigBtn = document.getElementById('reload-config-btn');
    if (reloadConfigBtn) {
        reloadConfigBtn.addEventListener('click', () => {
            const fileNameSpan = document.getElementById('config-file-name');
            loadConfigFile(fileNameSpan.textContent, currentConfigType);
        });
    }
    
    // Close modal when clicking outside
    const configModal = document.getElementById('config-modal');
    if (configModal) {
        configModal.addEventListener('click', (e) => {
            if (e.target === configModal) {
                closeConfigModal();
            }
        });
    }
    
    // Close modal with Escape key
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && configModal && configModal.style.display === 'flex') {
            closeConfigModal();
        }
    });
}

