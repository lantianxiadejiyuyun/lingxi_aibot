(function () {
  "use strict";

  var contextSelect = document.getElementById("llm_context_window_tokens");
  var contextCustom = document.getElementById("llm_context_window_custom");
  var contextGroup = document.getElementById("llm-context-custom-group");
  if (contextSelect && contextCustom && contextGroup) {
    function updateContextInput() {
      var custom = contextSelect.value === "custom";
      contextGroup.hidden = !custom;
      contextCustom.disabled = !custom;
      contextCustom.required = custom;
    }
    contextSelect.addEventListener("change", updateContextInput);
    updateContextInput();
  }

  var modelChoice = document.getElementById("llm-model-choice");
  var modelInput = document.getElementById("llm_model");
  var modelList = document.getElementById("llm_models");
  if (modelChoice && modelInput && modelList) {
    function refreshModelChoices() {
      var current = modelInput.value.trim();
      var models = [current].concat(modelList.value.split(/[\n,]/)).map(function (item) {
        return item.trim();
      }).filter(function (item, index, rows) {
        return item && rows.indexOf(item) === index;
      });
      modelChoice.innerHTML = "";
      models.forEach(function (model) {
        var option = document.createElement("option");
        option.value = model;
        option.textContent = model;
        option.selected = model === current;
        modelChoice.appendChild(option);
      });
      var custom = document.createElement("option");
      custom.value = "";
      custom.textContent = "手动填写模型 ID…";
      custom.selected = !current;
      modelChoice.appendChild(custom);
    }
    modelChoice.addEventListener("change", function () {
      if (modelChoice.value) modelInput.value = modelChoice.value;
      else modelInput.focus();
    });
    modelList.addEventListener("input", refreshModelChoices);
    modelInput.addEventListener("input", refreshModelChoices);
    // Provider shortcuts fill the same model input in the settings page listener.
    document.addEventListener("click", function (event) {
      if (event.target.closest("[data-llm-preset]")) setTimeout(refreshModelChoices, 0);
    });
    refreshModelChoices();
  }
})();
