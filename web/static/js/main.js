document.addEventListener('DOMContentLoaded', () => {
    // --- Elements ---
    const binInput = document.getElementById('binInput');
    const searchBtn = document.getElementById('searchBtn');
    
    const states = {
        welcome: document.getElementById('welcomeState'),
        loading: document.getElementById('loadingState'),
        error: document.getElementById('errorState'),
        result: document.getElementById('resultState')
    };

    let currentBin = '';

    // --- Formatters ---
    const formatCurrency = (val) => new Intl.NumberFormat('ru-RU').format(Math.round(val));

    // --- State Management ---
    function showState(stateName) {
        Object.values(states).forEach(el => el.classList.remove('active'));
        states[stateName].classList.add('active');
    }

    // --- API Calls ---
    async function fetchStatus() {
        try {
            const res = await fetch('/api/status');
            const data = await res.json();
            document.getElementById('authStatusIcon').textContent = data.active ? '🟢' : '🔴';
            document.getElementById('contractsCount').textContent = new Intl.NumberFormat('ru-RU').format(data.contracts_count);
            document.getElementById('specsCount').textContent = new Intl.NumberFormat('ru-RU').format(data.specs_count);
        } catch (e) {
            console.error(e);
        }
    }

    async function searchBin() {
        const val = binInput.value.replace(/\D/g, '');
        if (val.length !== 12) {
            showError("Неверный формат", "Введите 12 цифр БИН или ИИН.");
            return;
        }
        currentBin = val;
        showState('loading');
        
        try {
            // Load Analytics Overview
            const res = await fetch(`/api/analytics/${currentBin}`);
            if (!res.ok) {
                const errData = await res.json();
                throw new Error(errData.detail || "Контрагент не найден или ошибка сервера.");
            }
            const data = await res.json();
            
            if (data.error) {
                throw new Error(data.error);
            }
            
            const sup = data.supplier || {};
            const cust = data.customer || {};
            const companyName = sup.name || cust.name || "Неизвестно";
            
            document.getElementById('companyName').textContent = `Аналитика: ${companyName} (${data.bin})`;
            
            const totalContracts = (sup.total_contracts || 0) + (cust.total_contracts || 0);
            const totalAmount = (sup.total_amount || 0) + (cust.total_amount || 0);
            const okCount = (sup.open_tender_contracts || 0) + (cust.open_tender_contracts || 0);
            const zcpCount = (sup.zcp_contracts || 0) + (cust.zcp_contracts || 0);
            const oiCount = (sup.oi_contracts || 0) + (cust.oi_contracts || 0);
            
            // KPIs
            document.getElementById('kpiTotalCount').textContent = totalContracts;
            document.getElementById('kpiTotalSum').textContent = formatCurrency(totalAmount) + ' ₸';
            document.getElementById('kpiOkSum').textContent = okCount;
            document.getElementById('kpiZcpCount').textContent = zcpCount;
            document.getElementById('kpiOiCount').textContent = oiCount;

            // Counterparties Tab
            // Supplier
            document.getElementById('suppTotalSum').textContent = formatCurrency(sup.total_amount || 0) + ' ₸';
            document.getElementById('suppTotalCount').textContent = sup.total_contracts || 0;
            const topCustList = document.getElementById('topCustomersList');
            topCustList.innerHTML = '';
            (sup.counterparties || []).forEach(c => {
                const li = document.createElement('li');
                li.innerHTML = `<span>${c.name}</span> <strong>${formatCurrency(c.amount)} ₸</strong>`;
                topCustList.appendChild(li);
            });

            // Customer
            document.getElementById('custTotalSum').textContent = formatCurrency(cust.total_amount || 0) + ' ₸';
            document.getElementById('custTotalCount').textContent = cust.total_contracts || 0;
            const topSuppList = document.getElementById('topSuppliersList');
            topSuppList.innerHTML = '';
            (cust.counterparties || []).forEach(s => {
                const li = document.createElement('li');
                li.innerHTML = `<span>${s.name}</span> <strong>${formatCurrency(s.amount)} ₸</strong>`;
                topSuppList.appendChild(li);
            });

            // Fetch Tabs
            await loadContracts(currentBin);
            await loadSpecs(currentBin);

            showState('result');

        } catch (e) {
            showError("Ошибка загрузки", e.message);
        }
    }

    function showError(title, msg) {
        document.getElementById('errorTitle').textContent = title;
        document.getElementById('errorMessage').textContent = msg;
        showState('error');
    }

    async function loadContracts(bin) {
        const res = await fetch(`/api/contracts/${bin}`);
        const data = await res.json();
        const tbody = document.querySelector('#contractsTable tbody');
        tbody.innerHTML = '';
        data.contracts.forEach(c => {
            const tr = document.createElement('tr');
            
            let counterparty = '';
            if (bin === c.customer_bin) counterparty = `<div class="role-badge supplier"><i class="fa-solid fa-truck"></i> Поставщик</div><br>${c.supplier_name}`;
            else if (bin === c.supplier_bin) counterparty = `<div class="role-badge customer"><i class="fa-solid fa-building-columns"></i> Заказчик</div><br>${c.customer_name}`;
            else counterparty = `<div class="role-badge customer">З</div> ${c.customer_name}<br><div class="role-badge supplier">П</div> ${c.supplier_name}`;

            tr.innerHTML = `
                <td><code>${c.contract_number}</code></td>
                <td>${c.title}</td>
                <td>${c.method}</td>
                <td>${counterparty}</td>
                <td class="text-right">${formatCurrency(c.qty)}</td>
                <td class="text-right">${formatCurrency(c.unit_price)}</td>
                <td class="text-right text-success">${formatCurrency(c.amount)}</td>
            `;
            tbody.appendChild(tr);
        });
    }

    async function loadSpecs(bin) {
        const res = await fetch(`/api/specs/${bin}`);
        const data = await res.json();
        const tbody = document.querySelector('#specsTable tbody');
        tbody.innerHTML = '';
        data.specs.forEach(s => {
            const tr = document.createElement('tr');
            
            let counterparty = '';
            if (bin === s.customer_bin) counterparty = `<div class="role-badge supplier"><i class="fa-solid fa-truck"></i></div> ${s.supplier_name}`;
            else if (bin === s.supplier_bin) counterparty = `<div class="role-badge customer"><i class="fa-solid fa-building-columns"></i></div> ${s.customer_name}`;
            
            tr.innerHTML = `
                <td>${s.title}</td>
                <td><code>${s.brand || '-'}</code></td>
                <td>${s.country || '-'}</td>
                <td>${s.manufacturer || '-'}</td>
                <td>${counterparty}</td>
                <td class="text-right text-accent">${formatCurrency(s.amount)}</td>
            `;
            tbody.appendChild(tr);
        });
    }

    // --- Event Listeners ---
    searchBtn.addEventListener('click', searchBin);
    binInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') searchBin();
    });

    // Tabs
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            
            btn.classList.add('active');
            document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
        });
    });

    // Auth Modal
    const authModal = document.getElementById('authModal');
    document.getElementById('openAuthBtn').addEventListener('click', () => {
        authModal.classList.add('active');
        document.getElementById('authModalMsg').textContent = '';
    });
    document.getElementById('closeAuthModal').addEventListener('click', () => {
        authModal.classList.remove('active');
    });

    document.getElementById('saveAuthBtn').addEventListener('click', async () => {
        const session = document.getElementById('ciSessionInput').value;
        const msgEl = document.getElementById('authModalMsg');
        msgEl.textContent = 'Проверка...';
        
        try {
            const res = await fetch('/api/auth', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ci_session: session })
            });
            const data = await res.json();
            if (data.active) {
                msgEl.style.color = 'var(--accent)';
                msgEl.textContent = 'Успешно сохранено и активно!';
                fetchStatus();
                setTimeout(() => authModal.classList.remove('active'), 1500);
            } else {
                msgEl.style.color = 'var(--danger)';
                msgEl.textContent = 'Ошибка: ' + data.message;
            }
        } catch (e) {
            msgEl.textContent = e.message;
        }
    });

    // Excel
    document.getElementById('excelBtn').addEventListener('click', () => {
        if (currentBin) {
            window.location.href = `/api/excel/${currentBin}`;
        }
    });

    // Sync
    const syncModal = document.getElementById('syncModal');
    document.getElementById('syncBtn').addEventListener('click', async () => {
        if (!currentBin) return;
        syncModal.classList.add('active');
        try {
            await fetch(`/api/sync/${currentBin}`, { method: 'POST' });
            await searchBin(); // reload data
        } catch(e) {
            console.error(e);
        } finally {
            syncModal.classList.remove('active');
            fetchStatus();
        }
    });

    // Initial Status fetch
    fetchStatus();

    // Uptime Robot (Keep-Alive): Ping backend every 10 minutes to prevent Render from sleeping
    // while the user has this tab open.
    setInterval(fetchStatus, 10 * 60 * 1000);
});
