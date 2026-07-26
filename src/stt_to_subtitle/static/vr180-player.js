(() => {
  const VERTEX_SHADER = `
    attribute vec2 a_position;
    varying vec2 v_position;

    void main() {
      v_position = a_position;
      gl_Position = vec4(a_position, 0.0, 1.0);
    }
  `;

  const FRAGMENT_SHADER = `
    precision highp float;

    const float PI = 3.141592653589793;
    uniform sampler2D u_video;
    uniform float u_aspect;
    uniform float u_tan_half_fov;
    uniform float u_yaw;
    uniform float u_pitch;
    uniform float u_eye_offset;
    uniform float u_stereo_mode;
    varying vec2 v_position;

    void main() {
      float screen_x = v_position.x;
      float view_aspect = u_aspect;
      float eye_offset = u_eye_offset;
      if (u_stereo_mode > 0.5) {
        bool right_eye = v_position.x >= 0.0;
        screen_x = right_eye
          ? v_position.x * 2.0 - 1.0
          : v_position.x * 2.0 + 1.0;
        view_aspect = u_aspect * 0.5;
        eye_offset = right_eye ? 0.5 : 0.0;
      }

      vec3 view_direction = normalize(vec3(
        screen_x * view_aspect * u_tan_half_fov,
        v_position.y * u_tan_half_fov,
        -1.0
      ));

      float pitch_cos = cos(u_pitch);
      float pitch_sin = sin(u_pitch);
      vec3 pitched = vec3(
        view_direction.x,
        pitch_cos * view_direction.y - pitch_sin * view_direction.z,
        pitch_sin * view_direction.y + pitch_cos * view_direction.z
      );

      float yaw_cos = cos(u_yaw);
      float yaw_sin = sin(u_yaw);
      vec3 direction = vec3(
        yaw_cos * pitched.x - yaw_sin * pitched.z,
        pitched.y,
        yaw_sin * pitched.x + yaw_cos * pitched.z
      );

      float longitude = atan(direction.x, -direction.z);
      if (abs(longitude) > PI * 0.5) {
        gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0);
        return;
      }

      float latitude = asin(clamp(direction.y, -1.0, 1.0));
      float eye_u = longitude / PI + 0.5;
      float video_u = eye_offset + eye_u * 0.5;
      float video_v = 0.5 - latitude / PI;
      gl_FragColor = texture2D(u_video, vec2(video_u, video_v));
    }
  `;

  const clamp = (value, minimum, maximum) =>
    Math.min(maximum, Math.max(minimum, value));

  const compileShader = (gl, type, source) => {
    const shader = gl.createShader(type);
    if (!shader) {
      throw new Error("WebGL 셰이더를 만들 수 없습니다.");
    }
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const message = gl.getShaderInfoLog(shader) || "알 수 없는 셰이더 오류";
      gl.deleteShader(shader);
      throw new Error(message);
    }
    return shader;
  };

  const createProgram = (gl) => {
    const vertexShader = compileShader(
      gl,
      gl.VERTEX_SHADER,
      VERTEX_SHADER
    );
    const fragmentShader = compileShader(
      gl,
      gl.FRAGMENT_SHADER,
      FRAGMENT_SHADER
    );
    const program = gl.createProgram();
    if (!program) {
      throw new Error("WebGL 프로그램을 만들 수 없습니다.");
    }
    gl.attachShader(program, vertexShader);
    gl.attachShader(program, fragmentShader);
    gl.linkProgram(program);
    gl.deleteShader(vertexShader);
    gl.deleteShader(fragmentShader);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      const message = gl.getProgramInfoLog(program) || "알 수 없는 링크 오류";
      gl.deleteProgram(program);
      throw new Error(message);
    }
    return program;
  };

  const createRenderer = ({
    video,
    canvas,
    onContextLost = () => {},
  }) => {
    const gl = canvas.getContext("webgl", {
      alpha: false,
      antialias: true,
      powerPreference: "high-performance",
    });
    if (!gl) {
      throw new Error("이 브라우저에서 WebGL을 사용할 수 없습니다.");
    }

    const program = createProgram(gl);
    const positionLocation = gl.getAttribLocation(program, "a_position");
    const uniforms = {
      aspect: gl.getUniformLocation(program, "u_aspect"),
      eyeOffset: gl.getUniformLocation(program, "u_eye_offset"),
      pitch: gl.getUniformLocation(program, "u_pitch"),
      stereoMode: gl.getUniformLocation(program, "u_stereo_mode"),
      tanHalfFov: gl.getUniformLocation(program, "u_tan_half_fov"),
      video: gl.getUniformLocation(program, "u_video"),
      yaw: gl.getUniformLocation(program, "u_yaw"),
    };
    const positionBuffer = gl.createBuffer();
    const texture = gl.createTexture();
    if (!positionBuffer || !texture || positionLocation < 0) {
      throw new Error("WebGL 렌더링 자원을 만들 수 없습니다.");
    }

    gl.useProgram(program);
    gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([
        -1, -1,
        1, -1,
        -1, 1,
        -1, 1,
        1, -1,
        1, 1,
      ]),
      gl.STATIC_DRAW
    );
    gl.enableVertexAttribArray(positionLocation);
    gl.vertexAttribPointer(positionLocation, 2, gl.FLOAT, false, 0, 0);

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(
      gl.TEXTURE_2D,
      0,
      gl.RGBA,
      1,
      1,
      0,
      gl.RGBA,
      gl.UNSIGNED_BYTE,
      new Uint8Array([0, 0, 0, 255])
    );
    gl.uniform1i(uniforms.video, 0);

    let active = false;
    let animationFrame = null;
    let dirty = true;
    let eyeOffset = 0;
    let fieldOfView = 75;
    let pitch = 0;
    let stereoMode = 0;
    let yaw = 0;
    let pointerId = null;
    let pointerX = 0;
    let pointerY = 0;

    const resize = () => {
      const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
      const width = Math.max(1, Math.round(canvas.clientWidth * pixelRatio));
      const height = Math.max(
        1,
        Math.round(canvas.clientHeight * pixelRatio)
      );
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      gl.viewport(0, 0, width, height);
      return width / height;
    };

    const draw = () => {
      if (!active || gl.isContextLost()) {
        return;
      }
      const aspect = resize();
      if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
        gl.texImage2D(
          gl.TEXTURE_2D,
          0,
          gl.RGBA,
          gl.RGBA,
          gl.UNSIGNED_BYTE,
          video
        );
      }
      gl.useProgram(program);
      gl.uniform1f(uniforms.aspect, aspect);
      gl.uniform1f(uniforms.eyeOffset, eyeOffset);
      gl.uniform1f(uniforms.pitch, pitch);
      gl.uniform1f(uniforms.stereoMode, stereoMode);
      gl.uniform1f(
        uniforms.tanHalfFov,
        Math.tan((fieldOfView * Math.PI) / 360)
      );
      gl.uniform1f(uniforms.yaw, yaw);
      gl.drawArrays(gl.TRIANGLES, 0, 6);
    };

    const frame = () => {
      animationFrame = null;
      if (!active) {
        return;
      }
      if (dirty || (!video.paused && !video.ended)) {
        dirty = false;
        draw();
      }
      if (!video.paused && !video.ended) {
        animationFrame = window.requestAnimationFrame(frame);
      }
    };

    const requestDraw = () => {
      dirty = true;
      if (active && animationFrame === null) {
        animationFrame = window.requestAnimationFrame(frame);
      }
    };

    const reset = () => {
      fieldOfView = 75;
      pitch = 0;
      yaw = 0;
      requestDraw();
    };

    const onPointerDown = (event) => {
      pointerId = event.pointerId;
      pointerX = event.clientX;
      pointerY = event.clientY;
      canvas.setPointerCapture?.(event.pointerId);
      canvas.classList.add("is-dragging");
      canvas.focus({ preventScroll: true });
    };

    const onPointerMove = (event) => {
      if (event.pointerId !== pointerId) {
        return;
      }
      const deltaX = event.clientX - pointerX;
      const deltaY = event.clientY - pointerY;
      pointerX = event.clientX;
      pointerY = event.clientY;
      yaw = clamp(yaw - deltaX * 0.005, -Math.PI / 2, Math.PI / 2);
      pitch = clamp(
        pitch + deltaY * 0.005,
        -Math.PI * 0.47,
        Math.PI * 0.47
      );
      requestDraw();
    };

    const onPointerUp = (event) => {
      if (event.pointerId !== pointerId) {
        return;
      }
      canvas.releasePointerCapture?.(event.pointerId);
      pointerId = null;
      canvas.classList.remove("is-dragging");
    };

    const onWheel = (event) => {
      event.preventDefault();
      fieldOfView = clamp(fieldOfView + event.deltaY * 0.04, 35, 105);
      requestDraw();
    };

    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointercancel", onPointerUp);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener(
      "webglcontextlost",
      (event) => {
        event.preventDefault();
        active = false;
        if (animationFrame !== null) {
          window.cancelAnimationFrame(animationFrame);
          animationFrame = null;
        }
        onContextLost();
      },
      { once: true }
    );
    for (const eventName of [
      "loadeddata",
      "play",
      "seeked",
      "timeupdate",
    ]) {
      video.addEventListener(eventName, requestDraw);
    }
    window.addEventListener("resize", requestDraw);

    return {
      requestDraw,
      reset,
      setEye(eye) {
        stereoMode = eye === "dual" ? 1 : 0;
        eyeOffset = eye === "right" ? 0.5 : 0;
        requestDraw();
      },
      start() {
        active = true;
        requestDraw();
      },
      stop() {
        active = false;
        if (animationFrame !== null) {
          window.cancelAnimationFrame(animationFrame);
          animationFrame = null;
        }
      },
    };
  };

  window.createVR180Renderer = createRenderer;
})();
