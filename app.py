import streamlit as st
import pandas as pd
import math
import requests

# --- 1. PAGE CONFIG ---
st.set_page_config(
    page_title="Field Ops Optimizer",
    page_icon="🚛",
    layout="wide"
)

# --- 2. THE BRAIN ---
class RouteOptimizer:
    def __init__(self, office_coords, day_start=9.0):
        self.office = office_coords
        self.day_start = day_start 
        self.schedule = st.session_state.get('schedule', []) 
        self.candidates = [] 
        # Tuning Knobs
        self.DEFAULT_MEETING_TIME = 50 
        self.ROAD_BUFFER = 1.25     
        self.BASE_SPEED_KMPH = 30.0 
        self.KM_PER_MIN = self.BASE_SPEED_KMPH / 60.0 

    def _get_traffic_multiplier(self, hour_of_day):
        h = float(hour_of_day)
        if (8.5 <= h < 11.5) or (17.5 <= h < 20.5): return 2.5  
        elif 11.5 <= h < 17.5: return 1.8
        else: return 1.2

    def load_from_csv(self, file_path):
        try:
            try:
                df = pd.read_csv(file_path, encoding='utf-8')
            except UnicodeDecodeError:
                df = pd.read_csv(file_path, encoding='cp1252')
            
            if 'Internal/Status' in df.columns:
                # Robust string matching for status
                df = df[df['Internal/Status'].astype(str).str.contains("Inspection Pending", case=False, na=False)]
            
            self.candidates = [] 
            for index, row in df.iterrows():
                try:
                    raw_lat = str(row['Building/Lat']).replace('"', '').strip()
                    if ',' in raw_lat:
                        parts = raw_lat.split(',')
                        lat = float(parts[0].strip())
                        lon = float(parts[1].strip())
                    else:
                        lat = float(raw_lat)
                        lon = float(str(row['Building/Long']).replace('"', '').strip())

                    # Skip if NaN (Not a Number)
                    if math.isnan(lat) or math.isnan(lon):
                        continue

                    self.candidates.append({
                        'id': row['House_ID'], 
                        'coords': (lat, lon), 
                        'lat': lat, 
                        'lon': lon, 
                        'name': row.get('Building/Name', f"House {row['House_ID']}") 
                    })
                except (ValueError, IndexError): 
                    continue
                    
        except FileNotFoundError: st.error("❌ Error: 'Homes.csv' file not found.")

    def _get_dist_km(self, coords1, coords2):
        return math.sqrt((coords1[0]-coords2[0])**2 + (coords1[1]-coords2[1])**2) * 111.0

    def get_real_travel_time_batch(self, start_coords, end_coords, candidates, hour_of_day):
        traffic_factor = self._get_traffic_multiplier(hour_of_day)
        
        for c in candidates:
            url = f"http://router.project-osrm.org/route/v1/driving/{start_coords[1]},{start_coords[0]};{c['coords'][1]},{c['coords'][0]}?overview=false"
            try:
                r = requests.get(url, timeout=2)
                if r.status_code == 200:
                    raw_mins = r.json()['routes'][0]['duration'] / 60.0 
                    c['trip1'] = raw_mins * traffic_factor 
                else:
                    c['trip1'] = (self._get_dist_km(start_coords, c['coords']) / self.KM_PER_MIN) * traffic_factor
            except:
                 c['trip1'] = (self._get_dist_km(start_coords, c['coords']) / self.KM_PER_MIN) * traffic_factor

            if end_coords:
                url2 = f"http://router.project-osrm.org/route/v1/driving/{c['coords'][1]},{c['coords'][0]};{end_coords[1]},{end_coords[0]}?overview=false"
                try:
                    r = requests.get(url2, timeout=2)
                    if r.status_code == 200:
                         raw_mins = r.json()['routes'][0]['duration'] / 60.0
                         c['trip2'] = raw_mins * traffic_factor
                    else:
                         c['trip2'] = (self._get_dist_km(c['coords'], end_coords) / self.KM_PER_MIN) * traffic_factor
                except:
                    c['trip2'] = (self._get_dist_km(c['coords'], end_coords) / self.KM_PER_MIN) * traffic_factor
            else:
                c['trip2'] = 0

    def recommend_next_calls(self, skip=0):
        current_loc = self.office
        current_time = self.day_start
        skipped_count = 0
        def fmt(t): return f"{int(t)}:{int((t-int(t))*60):02d}"

        for job in self.schedule:
            gap_mins = (job['start'] - current_time) * 60
            min_gap = self.DEFAULT_MEETING_TIME + 15 
            
            if gap_mins > min_gap: 
                if skipped_count < skip:
                    skipped_count += 1
                    current_time = job['end']; current_loc = job['coords']
                    continue
                
                return {
                    'type': 'gap',
                    'msg': f"Found Gap: {fmt(current_time)} - {fmt(job['start'])} ({int(gap_mins)} mins)",
                    'start_node': current_loc,
                    'end_node': job['coords'],
                    'time': current_time,
                    'gap_duration': gap_mins
                }

            current_time = job['end']; current_loc = job['coords']

        return {
            'type': 'end',
            'msg': f"End of Day: Free after {fmt(current_time)}",
            'start_node': current_loc,
            'end_node': None,
            'time': current_time,
            'gap_duration': 999
        }

    def solve_recommendations(self, scenario):
        start_node = scenario['start_node']
        end_node = scenario['end_node']
        current_time = scenario['time']
        gap_mins = scenario['gap_duration']

        traffic_factor = self._get_traffic_multiplier(current_time)
        current_speed = self.BASE_SPEED_KMPH / traffic_factor
        travel_budget = gap_mins - self.DEFAULT_MEETING_TIME
        max_detour_km = travel_budget * (current_speed / 60.0)

        base_dist = 0
        if end_node:
            base_dist = self._get_dist_km(start_node, end_node)

        survivors = []
        for c in self.candidates:
            if any(j['id'] == c['id'] for j in self.schedule): continue

            d1 = self._get_dist_km(start_node, c['coords'])
            d2 = 0
            if end_node:
                d2 = self._get_dist_km(c['coords'], end_node)
            
            if end_node:
                if ((d1 + d2) * self.ROAD_BUFFER) < max_detour_km + base_dist:
                    c['math_score'] = (d1 + d2) - base_dist
                    survivors.append(c)
            else:
                c['math_score'] = d1
                survivors.append(c)

        shortlist = sorted(survivors, key=lambda x: x['math_score'])[:10]
        self.get_real_travel_time_batch(start_node, end_node, shortlist, hour_of_day=current_time)

        for c in shortlist: c['final_score'] = c['trip1'] + c['trip2']
        
        if end_node:
            valid = [c for c in shortlist if (c['final_score'] + self.DEFAULT_MEETING_TIME) < gap_mins]
            valid.sort(key=lambda x: x['final_score'])
            return valid
        else:
            shortlist.sort(key=lambda x: x['final_score'])
            return shortlist

# --- 3. UI LOGIC ---
if 'schedule' not in st.session_state: st.session_state.schedule = []

# Initialize Brain
optimizer = RouteOptimizer(office_coords=(12.9716, 77.5946), day_start=9.0)
optimizer.load_from_csv('Homes.csv')
optimizer.schedule = st.session_state.schedule

# --- SIDEBAR ---
with st.sidebar:
    st.header("📅 Today's Plan")
    if not st.session_state.schedule:
        st.info("No bookings yet.")
    else:
        sorted_sched = sorted(st.session_state.schedule, key=lambda x: x['start'])
        for item in sorted_sched:
            with st.container():
                st.markdown(f"**{int(item['start'])}:00 - {int(item['end'])}:00**")
                st.markdown(f"📍 {item['name']}")
                st.divider()
    
    if st.button("🗑️ Reset Day", type="primary"):
        st.session_state.schedule = []
        st.rerun()

# --- MAIN DASHBOARD ---
st.title("🚛 Field Ops Optimizer")

col1, col2, col3 = st.columns(3)
col1.metric("Pending Inspections", len(optimizer.candidates))
col2.metric("Scheduled Visits", len(st.session_state.schedule))
col3.metric("Traffic Status", "Heavy (2.5x)" if optimizer._get_traffic_multiplier(9) > 2 else "Moderate")

st.markdown("---")

col_ai_1, col_ai_2 = st.columns([1, 3])
with col_ai_1:
    st.subheader("⚙️ Controls")
    skip_val = st.number_input("Skip Gaps:", min_value=0, max_value=5, value=0, help="Skip early morning gaps.")
    
with col_ai_2:
    st.subheader("🧠 Recommendations")
    scenario = optimizer.recommend_next_calls(skip=skip_val)
    
    if scenario['type'] == 'gap':
        st.info(f"🧩 **{scenario['msg']}**")
    else:
        st.success(f"📍 **{scenario['msg']}**")
    
    results = optimizer.solve_recommendations(scenario)

    if results:
        # --- MAP VIEW (CRASH PROOF VERSION) ---
        clean_map_data = []
        for r in results:
            try:
                # FORCE FLOAT CONVERSION & CHECK NAN
                lat = float(r.get('lat', r['coords'][0]))
                lon = float(r.get('lon', r['coords'][1]))
                
                # Only add if Valid Number
                if not math.isnan(lat) and not math.isnan(lon):
                    clean_map_data.append({'lat': lat, 'lon': lon})
            except (ValueError, TypeError):
                continue
        
        if clean_map_data:
            st.map(pd.DataFrame(clean_map_data), size=20, zoom=11)
        else:
            st.warning("⚠️ Candidates found, but coordinates are invalid/missing for map.")
        
        # TABLE VIEW
        display_data = []
        for r in results:
            display_data.append({
                "Name": r['name'],
                "Added Travel (mins)": round(r['final_score'], 1),
                "ID": r['id']
            })
        st.dataframe(display_data, use_container_width=True)
        
        # BOOKING ACTION
        st.markdown("### ✅ Book a Slot")
        c1, c2 = st.columns([3, 1])
        with c1:
            selected_id = st.selectbox("Select Property:", options=[r['id'] for r in results], format_func=lambda x: next((r['name'] for r in results if r['id'] == x), x))
        with c2:
            default_time = scenario['time']
            if scenario['type'] == 'gap':
                default_time = math.ceil(scenario['time'] * 2) / 2
            
            book_time = st.number_input("Time (24h):", min_value=9.0, max_value=19.0, value=float(default_time), step=0.5)

        if st.button("Confirm Booking 🚀", type="primary", use_container_width=True):
            house = next((r for r in results if r['id'] == selected_id), None)
            if house:
                new_booking = {
                    'id': house['id'],
                    'name': house['name'],
                    'coords': house['coords'],
                    'start': book_time,
                    'end': book_time + (50/60.0),
                    'duration': 50
                }
                overlap = False
                for job in st.session_state.schedule:
                    if (new_booking['start'] < job['end']) and (new_booking['end'] > job['start']):
                        st.error(f"❌ Time Collision! You are busy at {job['name']}")
                        overlap = True
                        break
                
                if not overlap:
                    st.session_state.schedule.append(new_booking)
                    st.toast(f"Booked {house['name']}!", icon="✅")
                    st.rerun()
            
    else:
        st.warning("⚠️ No houses fit in this time slot/gap based on traffic conditions.")
