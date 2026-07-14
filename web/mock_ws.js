/* Browser mock feed. It uses the same event envelope as the WebSocket service. */
(function () {
  const projectId = "rover-demo";
  let sequence = 1;
  const emit = (handler, event) => handler({ project_id: projectId, seq: sequence++, ...event });
  function trajectory() {
    return Array.from({ length: 81 }, (_, i) => {
      const t = i / 8;
      return { t, base_pos: [-2.55 + i / 80 * 4.65, -.48 + Math.sin(i / 14) * .12, .12], base_quat: [0, 0, .027, .999], joints: { left_wheel: 7.8, right_wheel: 8.05 } };
    });
  }
  const frames = trajectory();
  function playDemo(handler, options) {
    const opts = { interval: 95, includeStages: true, ...(options || {}) };
    let stopped = false;
    const later = (fn, delay) => setTimeout(() => !stopped && fn(), delay);
    if (opts.includeStages) ["spec", "morphology", "build", "observe"].forEach((stage, index) => {
      const notes = { spec: "Turning your request into a rover specification.", morphology: "Applying the differential-drive template.", build: "Agent is preparing the project controller.", observe: "Running the project and collecting evidence." };
      later(() => emit(handler, { type: "stage", run_id: "run_12", stage, status: "start", note: notes[stage] }), index * 180);
      later(() => emit(handler, { type: "stage", run_id: "run_12", stage, status: "done", note: "Complete" }), index * 180 + 130);
    });
    later(() => emit(handler, { type: "chat", role: "assistant", text: "I’m validating the rover controller against the red-ball scenario. I’ll inspect telemetry and camera evidence if movement looks unexpected." }), 160);
    later(() => emit(handler, { type: "agent_step", role: "tool_call", summary: "Running batch simulation for 10 seconds" }), 410);
    frames.forEach((state, index) => later(() => {
      emit(handler, { type: "sim_state", run_id: "run_12", ...state });
      emit(handler, { type: "telemetry", run_id: "run_12", t: state.t, series: { dist_to_target: Math.max(.17, 4.72 - index * .057), yaw_drift: 2.7 + index * .008 } });
      if (index % 8 === 0) emit(handler, { type: "sim_frame", run_id: "run_12", t: state.t, jpeg_b64: "mock-frame" });
      if (index === 11) emit(handler, { type: "agent_step", role: "tool_result", summary: "Wheel velocity is balanced; target distance is decreasing." });
    }, 550 + index * opts.interval));
    later(() => {
      emit(handler, { type: "stage", run_id: "run_12", stage: "observe", status: "done", note: "Rover reached the target without collisions." });
      emit(handler, { type: "files", run_id: "run_12", tree: ["spec.json", "robot.urdf", "controller.py", "ros2_ws/"], changed: ["controller.py"] });
      emit(handler, { type: "agent_step", role: "edit", summary: "Validated controller.py against the successful run.", file: "controller.py", lines: [10, 16] });
      emit(handler, { type: "chat", role: "assistant", text: "Run 12 succeeded: the rover reached the target with 0 collisions. You can now ask me to tune the rover or edit a file directly." });
      emit(handler, { type: "run_summary", run_id: "run_12", artifact_revision: "rev_17", status: "task_success", telemetry_summary: { distance_to_target_m: .17, collisions: 0, sim_time_s: 10 } });
    }, 550 + frames.length * opts.interval + 120);
    return { stop: () => { stopped = true; } };
  }
  window.RoboPilotMock = { playDemo, trajectory };
})();
